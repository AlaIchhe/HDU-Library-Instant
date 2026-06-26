"""共享的 cookie 获取 / 持久化逻辑，供 web_app 和 scheduler 复用。

为什么独立成模块：
    - cookie 读取（DPAPI / CDP）可能在子进程里 native crash，必须用
      subprocess 隔离，避免拖垮主服务/调度线程。
    - web_app 和 scheduler 都需要这套逻辑，抽出来避免重复与循环依赖。
"""

import json
import subprocess
import sys
from pathlib import Path

TARGET_DOMAIN = "hdu.huitu.zhishulib.com"
PROBE_SCRIPT = Path(__file__).with_name("_cookie_probe.py")
COOKIE_FILE = Path(__file__).with_name("browser_cookie.json")
PROBE_TIMEOUT = 30


def safe_find_cookie(preferred_browser: str = "auto") -> tuple[str | None, list[str]]:
    """在子进程中调用 find_hdu_cookie，隔离 native crash。

    返回: (cookie_header_str | None, logs)
    """
    if not PROBE_SCRIPT.exists():
        # 探针缺失时退回直接调用（失去隔离，但仍可用）
        try:
            from cookie_browser import find_hdu_cookie
            return find_hdu_cookie(preferred_browser), []
        except Exception as exc:  # noqa: BLE001
            return None, [f"cookie 模块不可用：{exc}"]

    try:
        proc = subprocess.run(
            [sys.executable, str(PROBE_SCRIPT), preferred_browser],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
            cwd=str(PROBE_SCRIPT.parent),
        )
        logs = [line.strip() for line in proc.stderr.splitlines() if line.strip()]
        if proc.returncode != 0:
            logs.append(f"探针异常退出 (code={proc.returncode})，可能是浏览器数据不兼容")
            return None, logs
        if proc.stdout.strip():
            result = json.loads(proc.stdout.strip())
            return result.get("cookie"), logs
        return None, logs
    except subprocess.TimeoutExpired:
        return None, [f"探针超时（{PROBE_TIMEOUT} 秒）"]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, [f"探针异常: {exc}"]


def cookie_str_to_list(cookie_str: str) -> list[dict]:
    """把 "k1=v1; k2=v2" 形式的 cookie 头转成 instant_book 可读的列表。"""
    items = []
    for part in (cookie_str or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        items.append({
            "name": name,
            "value": value.strip(),
            "domain": TARGET_DOMAIN,
            "path": "/",
        })
    return items


def save_cookie_file(cookie_str: str, path: Path = COOKIE_FILE) -> bool:
    """把 cookie 头持久化为 JSON 文件（auth.cookie_file 可直接加载）。"""
    items = cookie_str_to_list(cookie_str)
    if not items:
        return False
    try:
        path.write_text(
            json.dumps({"cookies": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except OSError:
        return False
