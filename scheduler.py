"""
后台定时调度器：每日在配置的时间自动执行图书馆座位预约。

用法：
    scheduler = Scheduler(config_path)
    scheduler.start()          # 启动后台线程
    state = scheduler.get_state()  # 获取状态供 Web UI 显示
    scheduler.stop()           # 停止
"""

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from instant_book import (
    DEFAULT_BOOK_DAYS,
    DEFAULT_CONFIG,
    DEFAULT_MAX_TRIALS,
    DEFAULT_RETRY_DELAY,
    build_execute_time,
    load_config,
    normalize_execute_at,
    parse_plan,
    run_booking,
)

LOG_FILE = Path(__file__).with_name("booking.log")


class Scheduler:
    """每日自动预约调度器。"""

    def __init__(self, config_path=DEFAULT_CONFIG):
        self.config_path = Path(config_path)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

        # 状态字段（线程安全访问）
        self.enabled = False
        self.status = "idle"        # idle | waiting | running | done | error
        self.next_run = None        # datetime
        self.last_result = ""       # 上次执行结果摘要
        self.last_time = ""         # 上次执行时间
        self.current_logs: list[str] = []

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def start(self):
        """启动调度线程。"""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self.enabled = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()

    def stop(self):
        """停止调度线程。"""
        with self._lock:
            self.enabled = False
            self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def get_state(self) -> dict:
        """返回调度器当前状态（供 Web UI 轮询）。"""
        with self._lock:
            return {
                "enabled": self.enabled,
                "status": self.status,
                "next_run": self.next_run.strftime("%Y-%m-%d %H:%M:%S") if self.next_run else "",
                "next_run_ts": self.next_run.timestamp() if self.next_run else 0,
                "last_result": self.last_result,
                "last_time": self.last_time,
            }

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _run_loop(self):
        """调度主循环。

        正常路径：_run_once 内部计算下次执行时间并等待，返回后立即循环。
        异常路径：指数退避（30s → 60s → 120s → 240s → 300s 封顶），
                  成功后重置计数器，防止瞬态错误引发重试风暴。
        """
        consecutive_errors = 0
        while not self._stop_event.is_set():
            try:
                self._run_once()
                consecutive_errors = 0  # 成功执行后重置
            except Exception as exc:
                consecutive_errors += 1
                delay = min(300, 30 * (2 ** min(consecutive_errors - 1, 4)))
                self._set_state(
                    "error",
                    last_result=f"调度异常（#{consecutive_errors}，{delay}s 后重试）：{exc}",
                )
                if self._stop_event.wait(delay):
                    return
            else:
                # 一次执行完成后，等待到下一次
                if not self._stop_event.is_set():
                    pass  # _run_once 内部已经处理了等待

    def _run_once(self):
        """执行一次预约流程：计算下次时间 → 等待 → 执行。"""
        config = self._load_config()
        execute_at = self._get_execute_at(config)

        # 计算下次执行时间
        next_time = self._calc_next_run(execute_at)
        if next_time is None:
            self._set_state("error", last_result="无法解析 execute_at 时间")
            return

        self._set_state("waiting", next_run=next_time)
        self._wait_until(next_time)
        if self._stop_event.is_set():
            return

        # 执行预约
        self._set_state("running", next_run=next_time)
        self.current_logs = []
        result_msg = ""
        try:
            config = self._load_config()  # 重新读取，尊重网页修改
            booking = config.get("booking") or {}
            plan_text = str(booking.get("plan", ""))
            execute_at_str = normalize_execute_at(booking.get("execute_at"))
            max_trials = int(booking.get("max_trials", DEFAULT_MAX_TRIALS))
            retry_delay = float(booking.get("retry_delay", DEFAULT_RETRY_DELAY))

            # 预约前刷新 cookie：从专用 profile 读取最新登录态。
            # 读取成功则缓存到 browser_cookie.json 并直接使用；失败则回退到
            # config 里的 auth.cookie / cookie_file（上次缓存的那份）。
            browser_cookie = self._refresh_cookie(config)

            outcome = run_booking(
                config_path=self.config_path,
                plan_text=plan_text,
                days=0,  # 每天自动执行，预约"今天"
                dry_run_override=bool(booking.get("dry_run")),
                execute_at=None,  # 调度器自己控制时机，立即提交
                max_trials=max_trials,
                retry_delay=retry_delay,
                logger=self._log,
                should_cancel=self._stop_event.is_set,
                browser_cookie=browser_cookie,
            )

            if outcome.get("dry_run"):
                result_msg = f"dry-run 完成，座位 {outcome['seat_item']['title']} ({outcome['room_item']['name']})"
            else:
                from instant_book import booking_result_failed, booking_result_message
                res = outcome.get("result", {})
                if booking_result_failed(res):
                    result_msg = f"预约失败：{booking_result_message(res)}"
                else:
                    result_msg = f"预约成功：{booking_result_message(res)}"

            now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
            self._set_state("done", last_result=result_msg, last_time=now_str)

        except Exception as exc:
            now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
            self._set_state("error", last_result=f"失败：{exc}", last_time=now_str)

        # 持久化日志
        self._write_log()

    def _calc_next_run(self, execute_at_str: str) -> datetime | None:
        """计算下一次执行时间。"""
        return build_execute_time(execute_at_str)

    def _wait_until(self, target_time: datetime):
        """等待到目标时间（提前 2 秒结束，让 run_booking 自己精准控制）。"""
        while True:
            if self._stop_event.is_set():
                return
            now = datetime.now().astimezone()
            remaining = (target_time - now).total_seconds()
            if remaining <= 2:
                break
            if remaining > 60:
                sleep = 30
            elif remaining > 10:
                sleep = 5
            else:
                sleep = min(1, remaining - 2)
            self._stop_event.wait(max(sleep, 0.2))

    def _load_config(self):
        try:
            return load_config(self.config_path)
        except Exception:
            return {}

    def _get_execute_at(self, config: dict) -> str:
        booking = config.get("booking") or {}
        raw = booking.get("execute_at", "")
        return normalize_execute_at(raw)

    def _refresh_cookie(self, config: dict) -> str | None:
        """预约前刷新 cookie：从专用 profile 读取最新登录态并缓存。

        返回可用的 cookie 头字符串；读取失败返回 None（由 run_booking 回退到
        config 里的 auth.cookie / cookie_file）。
        """
        automation = config.get("automation") or {}
        if not automation.get("auto_cookie", True):
            return None

        preferred = automation.get("preferred_browser", "auto")
        try:
            from safe_cookie import safe_find_cookie, save_cookie_file
        except ImportError:
            return None

        self._log("正在刷新 cookie（读取专用 profile）...")
        cookie, logs = safe_find_cookie(preferred)
        for line in logs:
            self._log(line)
        if cookie:
            if save_cookie_file(cookie):
                self._log("已缓存最新 cookie 到 browser_cookie.json")
            return cookie
        self._log("未读到最新 cookie，将回退到已缓存的 cookie")
        return None

    def _log(self, message: str):
        """调度器内部日志回调，供 run_booking 使用。"""
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        with self._lock:
            self.current_logs.append(line)

    def _set_state(self, status: str, **kwargs):
        with self._lock:
            self.status = status
            for key, value in kwargs.items():
                if hasattr(self, key):
                    setattr(self, key, value)

    def _write_log(self):
        """将最近一次执行日志追加到 booking.log。"""
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*50}\n")
                f.write(f"执行时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"结果：{self.last_result}\n")
                f.write("日志：\n")
                for line in self.current_logs:
                    f.write(f"  {line}\n")
        except Exception:
            pass
