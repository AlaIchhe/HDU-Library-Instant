import base64
import hashlib
import json
import os
import time
from datetime import datetime, timedelta
from math import floor
from pathlib import Path
from urllib.parse import unquote

import requests
import yaml


DEFAULT_CONFIG = Path(__file__).with_name("config.yaml")
DEFAULT_BOOK_DAYS = 0
DEFAULT_MAX_TRIALS = 5
DEFAULT_RETRY_DELAY = 1.0
MSG_TIME_OUT_OF_RANGE = "超出可预约座位时间范围"

# 默认配置模板 —— 配置文件不存在时自动生成
_DEFAULT_CONFIG_YAML = """\
# HDU 图书馆即时预约配置（自动生成）
#

auth:
  # 从浏览器登录后，脚本会自动读取 cookie，通常不需要手动填写。
  cookie: ''
  # 自动读取成功时会把 cookie 缓存到此文件，作为读取失败时的兜底。
  cookie_file: browser_cookie.json

user_info:
  # 慧图系统内部 uid，不一定等于学号。留空则自动识别。
  uid: ''
  name: ''

booking:
  # 格式：1:floorId:seatNum:startHour:durationHours（roomType 固定为 1=自习室）
  # 当前默认：自习室、六楼杭韵数阁、130座、13:00 开始、9 小时。
  plan: 1:1559:130:13:9
  # 定时提交时间，格式 HH:MM 或 HH:MM:SS；默认等到 20:00 提交。
  # 清空表示立即提交。
  execute_at: '20:00:00'
  # 到点后最多重试次数。仅对"超出可预约座位时间范围"错误重试。
  max_trials: 5
  # 重试间隔秒数。
  retry_delay: 2
  # true 时只查询并打印，不真正提交预约。
  dry_run: false

request:
  timeout: 10

session:
  headers:
    Accept: application/json, text/plain, */*
    Accept-Encoding: gzip, deflate, br, zstd
    Accept-Language: zh-CN,zh;q=0.9,en;q=0.8
    Connection: keep-alive
    Content-type: application/x-www-form-urlencoded;charset=UTF-8
    Host: hdu.huitu.zhishulib.com
    Origin: https://hdu.huitu.zhishulib.com
    Referer: https://hdu.huitu.zhishulib.com/
    Sec-Fetch-Dest: empty
    Sec-Fetch-Mode: cors
    Sec-Fetch-Site: same-origin
    User-Agent: Mozilla/5.0 (Linux; Android 12; Pixel 3 Build/SP1A.210812.016.C2; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/86.0.4240.99 Mobile Safari/537.36 MicroMessenger/8.0.30 Language/zh_CN
  params:
    LAB_JSON: '1'
  trust_env: false
  verify: false

urls:
  book_seat: https://hdu.huitu.zhishulib.com/Seat/Index/bookSeats
  query_rooms: https://hdu.huitu.zhishulib.com/Space/Category/list
  query_seats: https://hdu.huitu.zhishulib.com/Seat/Index/searchSeats
  user_base_info: https://hdu.huitu.zhishulib.com/User/Center/baseInfo
  user_center: https://hdu.huitu.zhishulib.com/User/Center/index

# 自动化配置
automation:
  # 是否启用每日自动预约（网页里也能开关）
  auto_daily: true
  # 是否自动从浏览器读取 cookie
  auto_cookie: true
  # 优先使用的浏览器：chrome / edge / auto（自动检测）
  preferred_browser: auto
"""


def ensure_config(path=None):
    """确保配置文件存在，不存在则自动生成默认配置。

    始终在脚本所在目录创建 / 读取配置文件，不依赖用户目录。
    """
    target = Path(path) if path else DEFAULT_CONFIG
    target = target.resolve()

    if target.exists():
        return target

    # 自动生成默认配置
    target.write_text(_DEFAULT_CONFIG_YAML, encoding="utf-8")
    return target


class InstantBooker:
    def __init__(self, config):
        self.config = config
        self.urls = config["urls"]
        self.timeout = int(config.get("request", {}).get("timeout") or 10)
        self.session = requests.Session()
        self.session.headers.update(config["session"]["headers"])
        self.session.params = config["session"].get("params") or {"LAB_JSON": "1"}
        self.session.trust_env = bool(config["session"].get("trust_env", False))
        self.session.verify = bool(config["session"].get("verify", False))
        requests.packages.urllib3.disable_warnings()

        self.uid = str((config.get("user_info") or {}).get("uid") or "")
        self.name = str((config.get("user_info") or {}).get("name") or "")

    def load_cookies(self, browser_cookie=None):
        # 优先使用从浏览器自动读取的 cookie
        if browser_cookie:
            if self._load_cookie_header(browser_cookie):
                return

        auth = self.config.get("auth") or {}
        loaded = False
        if auth.get("cookie"):
            loaded = self._load_cookie_header(auth["cookie"]) or loaded
        if auth.get("cookie_file"):
            loaded = self._load_cookie_file(auth["cookie_file"]) or loaded
        if not loaded:
            raise RuntimeError("没有加载到 cookie。请在浏览器中登录并确保 automation.auto_cookie 开启，或手动填写 config.yaml 里的 auth.cookie。")

    def _load_cookie_header(self, cookie_header):
        self.session.headers["Cookie"] = cookie_header
        loaded = False
        for part in cookie_header.split(";"):
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not name:
                continue
            self.session.cookies.set(name, value, domain="hdu.huitu.zhishulib.com", path="/")
            loaded = True
        return loaded

    def _load_cookie_file(self, cookie_file):
        path = Path(os.path.expanduser(cookie_file))
        if not path.is_absolute():
            # 相对路径优先相对脚本目录解析（自启动时 cwd 可能不同），
            # 再回退到当前工作目录。
            script_dir = DEFAULT_CONFIG.parent
            if (script_dir / path).exists():
                path = script_dir / path
            else:
                path = Path.cwd() / path
        if not path.exists():
            # 缓存文件还没生成属正常情况（首次运行），不视为错误。
            return False

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        self._apply_user_info_candidate(self._find_user_info(data))
        return self._load_cookie_json(data)

    def _load_cookie_json(self, data):
        cookies = data.get("cookies") if isinstance(data, dict) else data
        if not isinstance(cookies, list):
            return False

        loaded = False
        for item in cookies:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if not name or value is None:
                continue
            cookie = requests.cookies.create_cookie(
                name=str(name),
                value=str(value),
                domain=item.get("domain") or "hdu.huitu.zhishulib.com",
                path=item.get("path") or "/",
                secure=bool(item.get("secure", False)),
            )
            self.session.cookies.set_cookie(cookie)
            loaded = True
        return loaded

    def resolve_user(self):
        if self.uid:
            return
        for key in ("user_base_info", "user_center"):
            url = self.urls.get(key)
            if not url:
                continue
            data = self.request("GET", url)
            if self._apply_user_info_candidate(self._find_user_info(data)):
                return
        raise RuntimeError("未能识别用户 uid。请在 config.yaml 的 user_info.uid 中填写慧图内部 uid。")

    def _apply_user_info_candidate(self, candidate):
        if not candidate or not candidate.get("uid"):
            return False
        if not self.uid:
            self.uid = str(candidate["uid"])
        if not self.name and candidate.get("name"):
            self.name = str(candidate["name"])
        return True

    def _find_user_info(self, data):
        candidates = []

        def walk(obj, hint=""):
            if isinstance(obj, dict):
                if "name" in obj and "value" in obj and isinstance(obj.get("value"), str):
                    walk(obj["value"], str(obj.get("name") or hint))
                candidate = self._user_info_from_dict(obj, hint)
                if candidate:
                    candidates.append(candidate)
                for key, value in obj.items():
                    walk(value, f"{hint}.{key}" if hint else str(key))
            elif isinstance(obj, list):
                for item in obj:
                    walk(item, hint)
            elif isinstance(obj, str):
                value = obj.strip()
                if value and value[0] in "[{":
                    try:
                        walk(json.loads(value), hint)
                    except Exception:
                        pass

        walk(data)
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.get("score", 0), reverse=True)
        return candidates[0]

    def _user_info_from_dict(self, data, hint=""):
        id_keys = ("uid", "user_id", "userId", "booker", "id")
        name_keys = ("name", "real_name", "realName", "bookerName", "username", "login_name", "nickname")
        uid = None
        name = None
        for key in id_keys:
            value = data.get(key)
            if value is not None and str(value).isdigit():
                uid = str(value)
                break
        for key in name_keys:
            value = data.get(key)
            if value:
                name = str(value)
                break

        score = 1 if name else 0
        hint = hint.lower()
        for keyword in ("current", "user", "login", "lab4"):
            if keyword in hint:
                score += 2
        if uid and (score > 0 or name):
            return {"uid": uid, "name": name, "score": score}
        return None

    def request(self, method, url, data=None):
        if method == "GET":
            response = self.session.get(url, timeout=self.timeout)
        else:
            response = self.session.post(url, data=data, timeout=self.timeout)
        if response.status_code not in (200, 302):
            raise RuntimeError(f"请求失败：HTTP {response.status_code} {url}")
        try:
            return response.json()
        except Exception as exc:
            raise RuntimeError(f"JSON 解析失败：{exc}") from exc

    def query_room_items(self):
        data = self.request("GET", self.urls["query_rooms"])
        raw_items = data["content"]["children"][1]["defaultItems"]
        room_items = []
        for item in raw_items:
            url = unquote(item["link"]["url"])
            query = url.split("?", 1)[1]
            room_items.append({"name": item["name"], "query": query})
        return room_items

    def query_room_detail(self, room_item):
        data = self.request("GET", self.urls["query_seats"] + "?" + room_item["query"])
        detail = data.get("data")
        if not detail:
            raise RuntimeError(f"房间信息为空：{room_item['name']}")
        return detail

    def validate_booking_time(self, room_detail, start_hour, duration_hours):
        range_info = room_detail.get("range") or {}
        min_begin = range_info.get("minBeginTime")
        max_end = range_info.get("maxEndTime")
        if min_begin is None or max_end is None:
            return
        min_begin = int(min_begin)
        max_end = int(max_end)
        end_hour = int(start_hour) + int(duration_hours)
        if int(start_hour) < min_begin or int(start_hour) >= max_end:
            raise RuntimeError(f"开始小时不在可预约范围内：允许 {min_begin}:00-{max_end}:00")
        if end_hour > max_end:
            raise RuntimeError(f"预约结束时间超出范围：允许最晚到 {max_end}:00")

    def query_seat_map(self, room_detail, begin_time, duration_hours, target_floor_id=None, logger=None):
        candidates = []
        seen_candidates = set()

        def add_candidate(label, when, hours):
            candidate = (
                label,
                when.replace(minute=0, second=0, microsecond=0),
                max(1, int(hours)),
            )
            key = (int(candidate[1].timestamp()), candidate[2])
            if key not in seen_candidates:
                seen_candidates.add(key)
                candidates.append(candidate)

        add_candidate("目标完整时段", begin_time, duration_hours)
        add_candidate("目标开始1小时", begin_time, 1)
        add_candidate("目标日08:00", begin_time.replace(hour=8), 1)

        now = datetime.now().astimezone()
        if now.hour >= 22:
            lookup_time = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        elif now.hour < 7:
            lookup_time = now.replace(hour=8, minute=0, second=0, microsecond=0)
        else:
            lookup_time = now
        add_candidate("当前可用时段", lookup_time, 1)

        merged = []
        seen_floor_ids = set()
        last_error = None
        for label, lookup_time, hours in candidates:
            try:
                floors = self._query_seat_map_once(room_detail, lookup_time, hours)
            except Exception as exc:
                last_error = exc
                if logger:
                    logger(f"座位图查询[{label}]失败：{exc}")
                continue
            if logger:
                logger(f"座位图查询[{label}]：获取 {len(floors)} 个楼层/区域")
            for floor in floors:
                floor_id = str(floor.get("seatMap", {}).get("info", {}).get("id"))
                if floor_id and floor_id not in seen_floor_ids:
                    seen_floor_ids.add(floor_id)
                    merged.append(floor)
            if target_floor_id and any(
                str(floor.get("seatMap", {}).get("info", {}).get("id")) == str(target_floor_id)
                for floor in floors
            ):
                return floors
            if not target_floor_id:
                return floors

        if merged:
            return merged
        if last_error:
            raise last_error
        raise RuntimeError("座位分布查询为空")

    def _query_seat_map_once(self, room_detail, lookup_time, duration_hours):
        payload = {
            "beginTime": lookup_time.timestamp(),
            "duration": duration_hours * 3600,
            "num": 1,
            "space_category[category_id]": room_detail["space_category"]["category_id"],
            "space_category[content_id]": room_detail["space_category"]["content_id"],
        }
        data = self.request("POST", self.urls["query_seats"], payload)
        try:
            return data["allContent"]["children"][2]["children"]["children"]
        except Exception as exc:
            raise RuntimeError(f"座位分布解析失败：{exc}") from exc

    def find_seat(self, floors, floor_id, seat_num):
        floor_id = str(floor_id)
        seat_num = str(seat_num)
        target_floor = None
        for item in floors:
            info = item.get("seatMap", {}).get("info", {})
            if str(info.get("id")) == floor_id:
                target_floor = item
                break
        if not target_floor:
            available = ", ".join(
                f"{item.get('roomName')}={item.get('seatMap', {}).get('info', {}).get('id')}"
                for item in floors
            )
            raise RuntimeError(f"找不到楼层 id={floor_id}。可用楼层：{available}")

        seats = target_floor["seatMap"]["POIs"]
        matches = [item for item in seats if str(item.get("title")) == seat_num]
        if not matches:
            raise RuntimeError(f"{target_floor.get('roomName')} 中找不到 {seat_num} 座")
        if len(matches) > 1:
            raise RuntimeError(f"{target_floor.get('roomName')} 中存在多个 {seat_num} 座")
        return target_floor, matches[0]

    def book(self, seat_id, begin_time, duration_hours, dry_run=False):
        payload = {
            "beginTime": int(begin_time.timestamp()),
            "duration": duration_hours * 3600,
            "is_recommend": 1,
            "api_time": floor(datetime.now().astimezone().timestamp()),
            "seats[0]": str(seat_id),
            "seatBookers[0]": str(self.uid),
        }
        token_source = (
            "post&/Seat/Index/bookSeats?LAB_JSON=1"
            f"&api_time{payload['api_time']}"
            f"&beginTime{payload['beginTime']}"
            f"&duration{payload['duration']}"
            f"&is_recommend{payload['is_recommend']}"
            f"&seatBookers[0]{payload['seatBookers[0]']}"
            f"&seats[0]{payload['seats[0]']}"
        )
        md5 = hashlib.md5(token_source.encode("utf-8")).hexdigest()
        self.session.headers["Api-Token"] = base64.b64encode(md5.encode("utf-8")).decode("utf-8")

        if dry_run:
            return {"dry_run": True, "payload": payload}
        return self.request("POST", self.urls["book_seat"], payload)


def load_config(path=None):
    """加载配置文件。文件不存在时自动生成默认配置。"""
    target = ensure_config(path)
    with target.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def parse_plan(plan_text):
    try:
        room_type, floor_id, seat_num, start_hour, duration_hours = plan_text.split(":")
        return {
            "room_type": int(room_type),
            "floor_id": int(floor_id),
            "seat_num": str(seat_num),
            "start_hour": int(start_hour),
            "duration_hours": int(duration_hours),
        }
    except Exception as exc:
        raise ValueError("plan 格式应为 roomType:floorId:seatNum:startHour:durationHours") from exc


def build_begin_time(start_hour, book_days):
    now = datetime.now().astimezone()
    return (now + timedelta(days=book_days)).replace(
        hour=start_hour,
        minute=0,
        second=0,
        microsecond=0,
    )


def parse_execute_at(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    raise ValueError("execute_at 格式应为 HH:MM 或 HH:MM:SS")


def normalize_execute_at(value):
    parsed = parse_execute_at(value)
    if parsed is None:
        return ""
    return f"{parsed.hour:02d}:{parsed.minute:02d}:{parsed.second:02d}"


def build_execute_time(execute_at, now=None):
    parsed = parse_execute_at(execute_at)
    if parsed is None:
        return None
    now = now or datetime.now().astimezone()
    target = now.replace(
        hour=parsed.hour,
        minute=parsed.minute,
        second=parsed.second,
        microsecond=0,
    )
    if target <= now:
        target += timedelta(days=1)
    return target


def booking_result_message(result):
    if not isinstance(result, dict):
        return "预约接口返回失败"
    data = result.get("DATA") if isinstance(result.get("DATA"), dict) else {}
    return str(result.get("MESSAGE") or data.get("msg") or "预约接口返回失败").strip()


def booking_result_failed(result):
    if not isinstance(result, dict):
        return True
    data = result.get("DATA") if isinstance(result.get("DATA"), dict) else {}
    code = str(result.get("CODE") or "").strip().lower()
    status = str(data.get("result") or "").strip().lower()
    return status == "fail" or code in {"paramerror", "error", "fail", "failed"}


def is_time_out_of_range(result):
    return MSG_TIME_OUT_OF_RANGE in booking_result_message(result)


def validate_booking_result(result):
    failed = booking_result_failed(result)
    message = booking_result_message(result)
    if not failed:
        return

    hint = ""
    if MSG_TIME_OUT_OF_RANGE in message:
        hint = "。这通常表示预约入口还没开放；请像 Master 一样设置执行时间，例如 20:00:00，到点后再提交。"
    raise RuntimeError(f"预约失败：{message}{hint}")


def wait_until(execute_time, logger=print, should_cancel=None):
    if execute_time is None:
        return
    logger(f"定时提交：将在 {execute_time.strftime('%Y-%m-%d %H:%M:%S')} 执行")
    next_notice_at = 0
    while True:
        if should_cancel and should_cancel():
            raise RuntimeError("任务已取消")
        now = datetime.now().astimezone()
        remaining = (execute_time - now).total_seconds()
        if remaining <= 0:
            break
        current_monotonic = time.monotonic()
        if current_monotonic >= next_notice_at:
            logger(f"未到执行时间，还差 {int(remaining)} 秒，等待中...")
            next_notice_at = current_monotonic + 60
        time.sleep(min(max(remaining, 0.2), 1 if remaining <= 10 else 10))
    logger("已到执行时间，开始提交")


def run_booking(
    config_path=DEFAULT_CONFIG,
    plan_text=None,
    days=None,
    dry_run_override=None,
    execute_at=None,
    max_trials=None,
    retry_delay=None,
    logger=print,
    should_cancel=None,
    browser_cookie=None,
):
    config = load_config(config_path)
    booking_cfg = config.get("booking") or {}
    plan_text = plan_text or str(booking_cfg.get("plan") or "")
    plan = parse_plan(plan_text)
    config_days = booking_cfg.get("book_days")
    book_days = days if days is not None else int(DEFAULT_BOOK_DAYS if config_days is None else config_days)
    dry_run = bool(booking_cfg.get("dry_run")) if dry_run_override is None else bool(dry_run_override)
    if execute_at is None:
        execute_at = booking_cfg.get("execute_at")
    execute_time = build_execute_time(execute_at)
    max_trials = int(max_trials if max_trials is not None else booking_cfg.get("max_trials", DEFAULT_MAX_TRIALS))
    retry_delay = float(retry_delay if retry_delay is not None else booking_cfg.get("retry_delay", DEFAULT_RETRY_DELAY))
    max_trials = max(1, min(max_trials, 20))
    retry_delay = max(0.2, min(retry_delay, 10.0))

    def check_cancel():
        if should_cancel and should_cancel():
            raise RuntimeError("任务已取消")

    begin_time = build_begin_time(plan["start_hour"], book_days)
    now = datetime.now().astimezone()
    if begin_time <= now:
        message = f"提醒：预约开始时间 {begin_time.strftime('%Y-%m-%d %H:%M')} 已不晚于当前时间，接口可能拒绝。"
        logger(message)
        if not dry_run:
            raise RuntimeError("预约开始时间已经过去，请改成当前时间之后，或选择其他预约日期。")

    check_cancel()
    booker = InstantBooker(config)
    logger("正在加载 cookie...")
    booker.load_cookies(browser_cookie=browser_cookie)
    check_cancel()
    logger("正在识别登录用户...")
    booker.resolve_user()
    logger(f"登录态已加载，uid={booker.uid}，name={booker.name or '-'}")

    check_cancel()
    logger("正在查询房间类型...")
    room_items = booker.query_room_items()
    if plan["room_type"] < 1 or plan["room_type"] > len(room_items):
        for index, item in enumerate(room_items, 1):
            logger(f"{index}. {item['name']}")
        raise RuntimeError(f"房间类型 {plan['room_type']} 不存在")

    room_item = room_items[plan["room_type"] - 1]
    logger(f"选择房间类型：{room_item['name']}")
    check_cancel()
    logger("正在查询房间详情...")
    room_detail = booker.query_room_detail(room_item)
    booker.validate_booking_time(room_detail, plan["start_hour"], plan["duration_hours"])
    check_cancel()
    logger("正在查询座位图...")
    floors = booker.query_seat_map(
        room_detail,
        begin_time,
        plan["duration_hours"],
        target_floor_id=plan["floor_id"],
        logger=logger,
    )
    check_cancel()
    logger("正在定位目标座位...")
    floor_item, seat_item = booker.find_seat(floors, plan["floor_id"], plan["seat_num"])
    logger(
        "目标座位："
        f"{room_item['name']} / {floor_item.get('roomName')} / {seat_item.get('title')}座 "
        f"(seatId={seat_item.get('id')})"
    )
    logger(f"目标时间：{begin_time.strftime('%Y-%m-%d %H:%M')}，{plan['duration_hours']} 小时")

    wait_until(execute_time, logger=logger, should_cancel=should_cancel)
    check_cancel()

    result = None
    if dry_run:
        logger("正在生成预约请求...")
        result = booker.book(seat_item["id"], begin_time, plan["duration_hours"], dry_run=True)
        logger("dry-run：已跳过提交预约。")
        logger(json.dumps(result["payload"], ensure_ascii=False, indent=2))
    else:
        for trial in range(1, max_trials + 1):
            check_cancel()
            logger(f"正在提交预约请求...[try={trial}/{max_trials}]")
            result = booker.book(seat_item["id"], begin_time, plan["duration_hours"], dry_run=False)
            logger("预约接口返回：")
            logger(json.dumps(result, ensure_ascii=False, indent=2))
            if not booking_result_failed(result):
                break
            if is_time_out_of_range(result) and trial < max_trials:
                logger(f"预约入口暂未开放，{retry_delay:g} 秒后重试...")
                end_wait = time.monotonic() + retry_delay
                while time.monotonic() < end_wait:
                    check_cancel()
                    time.sleep(min(0.2, end_wait - time.monotonic()))
                continue
            validate_booking_result(result)

    return {
        "plan": plan,
        "book_days": book_days,
        "dry_run": dry_run,
        "execute_at": normalize_execute_at(execute_at),
        "execute_time": execute_time,
        "max_trials": max_trials,
        "retry_delay": retry_delay,
        "begin_time": begin_time,
        "room_item": room_item,
        "floor_item": floor_item,
        "seat_item": seat_item,
        "result": result,
    }
