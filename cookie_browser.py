"""
从 Chrome / Edge 本地数据库自动提取 hdu.huitu.zhishulib.com 的 cookie。

原理：
  浏览器将 cookie 存储在 SQLite 中，值经 AES-256-GCM 加密。
  加密密钥存储在 Local State 的 os_crypt.encrypted_key 字段中，
  该字段又经 Windows DPAPI 保护。

依赖：
  - cryptography (AES-GCM 解密)
  - ctypes (Windows DPAPI)
  - sqlite3 / json / shutil / tempfile (标准库)
"""

import base64
import ctypes
import json
import os
import shutil
import sqlite3
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

# ---------------------------------------------------------------------------
# Windows DPAPI 解密
# ---------------------------------------------------------------------------

class _DATA_BLOB(ctypes.Structure):
    # pbData 必须是真正的指针类型（POINTER(c_char)），不能用 c_char_p。
    # c_char_p 会把内容当作 NUL 结尾的 C 字符串处理，而 DPAPI 密钥是含 \x00
    # 的二进制数据，会被截断；同时在 64 位 Python 下还需通过 argtypes/restype
    # 告知 ctypes 指针为 64 位，否则指针被截成 32 位 int，导致堆破坏(0xC0000374)。
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


# 声明 Win32 API 签名，确保 64 位下指针不被截断。
_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB),   # pDataIn
    ctypes.c_void_p,              # ppszDataDescr
    ctypes.POINTER(_DATA_BLOB),   # pOptionalEntropy
    ctypes.c_void_p,              # pvReserved
    ctypes.c_void_p,              # pPromptStruct
    wintypes.DWORD,               # dwFlags
    ctypes.POINTER(_DATA_BLOB),   # pDataOut
]
_crypt32.CryptUnprotectData.restype = wintypes.BOOL

_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p


def _dpapi_decrypt(encrypted_data: bytes) -> bytes | None:
    """使用 Windows DPAPI 解密数据。"""
    try:
        buffer = ctypes.create_string_buffer(encrypted_data, len(encrypted_data))
        blob_in = _DATA_BLOB(
            len(encrypted_data),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)),
        )
        blob_out = _DATA_BLOB()

        if _crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(blob_out),
        ):
            result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            _kernel32.LocalFree(blob_out.pbData)
            return result
        return None
    except (OSError, ValueError, RuntimeError):
        return None


# ---------------------------------------------------------------------------
# AES-256-GCM 解密 (Chrome v80+ cookie)
# ---------------------------------------------------------------------------

def _decrypt_cookie_value(encrypted_value: bytes, key: bytes) -> bytes | None:
    """解密 Chrome v80+ 的 AES-256-GCM 加密 cookie 值。

    encrypted_value 格式: b"v10" + nonce(12) + ciphertext+tag
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return None

    if encrypted_value.startswith(b"v10") or encrypted_value.startswith(b"v11"):
        prefix_len = 3
    else:
        return None

    data = encrypted_value[prefix_len:]
    if len(data) < 28:  # nonce(12) + tag(16) 最少
        return None

    nonce = data[:12]
    ciphertext_with_tag = data[12:]

    try:
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ciphertext_with_tag, None)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 浏览器发现 & 密钥提取
# ---------------------------------------------------------------------------

def _get_browser_candidates() -> list[dict]:
    """返回已安装浏览器候选列表，按优先级排序（Chrome > Edge > Chromium）。"""
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    candidates = []

    browsers = [
        {
            "name": "Chrome",
            "family": "chrome",
            "user_data": os.path.join(local_appdata, "Google", "Chrome", "User Data"),
        },
        {
            "name": "Edge",
            "family": "edge",
            "user_data": os.path.join(local_appdata, "Microsoft", "Edge", "User Data"),
        },
        {
            "name": "Chromium",
            "family": "chromium",
            "user_data": os.path.join(local_appdata, "Chromium", "User Data"),
        },
    ]

    for browser in browsers:
        user_data = Path(browser["user_data"])
        local_state = user_data / "Local State"
        if not local_state.exists():
            continue

        # 一个浏览器可能有多个 profile（Default / Profile 1 / Profile 2 ...），
        # 每个 profile 有独立的 Cookies 库，但共用 Local State 里的 AES 密钥。
        # 逐个 profile 作为候选，避免只扫 Default 而漏掉登录所在的 profile。
        profile_dirs = [user_data / "Default"]
        for entry in sorted(user_data.glob("Profile *")):
            if entry.is_dir():
                profile_dirs.append(entry)

        for profile_dir in profile_dirs:
            cookies_db = profile_dir / "Network" / "Cookies"
            if cookies_db.exists():
                candidates.append({
                    "name": f"{browser['name']} ({profile_dir.name})",
                    "family": browser["family"],
                    "user_data": browser["user_data"],
                    "profile": profile_dir.name,
                    "local_state": local_state,
                    "cookies_db": cookies_db,
                })

    return candidates


def _extract_aes_key(local_state_path: Path) -> bytes | None:
    """从浏览器的 Local State 文件中提取并解密 AES 密钥。"""
    try:
        with open(local_state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    encrypted_key_b64 = (
        state.get("os_crypt", {}).get("encrypted_key", "")
    )
    if not encrypted_key_b64 or not isinstance(encrypted_key_b64, str):
        return None

    try:
        encrypted_key = base64.b64decode(encrypted_key_b64)
    except (ValueError, TypeError):
        return None

    # Chrome 的 encrypted_key 以 "DPAPI" 前缀开头
    if encrypted_key.startswith(b"DPAPI"):
        encrypted_key = encrypted_key[5:]
    elif encrypted_key.startswith(b"DPAP"):
        # 偶见变体
        encrypted_key = encrypted_key[4:]

    if len(encrypted_key) < 32:  # 密钥不可能太短
        return None

    return _dpapi_decrypt(encrypted_key)


# ---------------------------------------------------------------------------
# Cookie 读取 (从 SQLite)
# ---------------------------------------------------------------------------

TARGET_DOMAIN = "hdu.huitu.zhishulib.com"


def _read_cookies_from_db(db_path: Path, aes_key: bytes) -> tuple[list[dict], bool]:
    """从 cookie SQLite 数据库中读取目标域名 cookie。

    返回 (cookies, saw_v20)。saw_v20 为 True 表示数据库里存在 v20
    （App-Bound 加密）的目标 cookie，离线无法解密，需要走 CDP 路径。
    """
    # 浏览器运行时会锁定数据库，先复制到临时文件
    tmp_dir = tempfile.mkdtemp(prefix="hdu_cookies_")
    tmp_db = os.path.join(tmp_dir, "Cookies")
    try:
        shutil.copy2(str(db_path), tmp_db)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return [], False

    try:
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT host_key, name, encrypted_value, path, expires_utc, is_secure "
            "FROM cookies "
            "WHERE host_key LIKE ? OR host_key = ?",
            (f"%{TARGET_DOMAIN}%", TARGET_DOMAIN),
        )
        rows = cursor.fetchall()
        conn.close()
    except Exception:
        rows = []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    cookies = []
    saw_v20 = False
    for row in rows:
        encrypted_value = row["encrypted_value"]
        if encrypted_value and bytes(encrypted_value[:3]) == b"v20":
            saw_v20 = True
        decrypted = _decrypt_cookie_value(encrypted_value, aes_key)
        if decrypted is not None:
            cookies.append({
                "name": row["name"],
                "value": decrypted.decode("utf-8", errors="replace"),
                "domain": row["host_key"],
                "path": row["path"],
                "secure": bool(row["is_secure"]),
            })
    return cookies, saw_v20


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def _find_via_dedicated_profile(preferred_browser: str, log) -> str | None:
    """尝试从工具专用 profile 读取 cookie（headless + CDP）。"""
    try:
        from cdp_cookie import dedicated_profile_dir, read_dedicated_cookies
    except ImportError:
        return None

    profile_dir = dedicated_profile_dir()
    if not profile_dir.exists():
        return None  # 尚未登录过专用 profile，走常规扫描

    log("检测到专用 profile，尝试读取其 cookie...")
    cookies = read_dedicated_cookies(TARGET_DOMAIN, preferred_browser, logger=log)
    if not cookies:
        log("专用 profile 未读到目标 cookie（可能登录已过期，请重新登录图书馆）")
        return None

    log(f"专用 profile 读取成功，找到 {len(cookies)} 个 cookie")
    for c in cookies:
        value = c["value"]
        log(f"    {c['name']}={value[:20]}{'...' if len(value) > 20 else ''}")
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    log(f"Cookie 提取成功 ({len(cookie_str)} 字符)")
    return cookie_str


def find_hdu_cookie(preferred_browser: str = "auto", logger=None) -> str | None:
    """从浏览器中查找目标域名的 cookie，返回可直接用作 Cookie 头的字符串。

    参数:
        preferred_browser: "chrome" | "edge" | "chromium" | "auto"
        logger:          可选，进度回调函数 log(message)

    返回:
        "key1=val1; key2=val2; ..." 格式的 cookie 字符串，
        失败返回 None
    """
    log = logger or (lambda msg: None)

    # 优先：专用 profile（工具独占，与日常浏览器隔离，不需要关用户的浏览器）
    cookie_str = _find_via_dedicated_profile(preferred_browser, log)
    if cookie_str:
        return cookie_str

    log("正在扫描本地浏览器...")
    candidates = _get_browser_candidates()

    if not candidates:
        log("未找到 Chrome / Edge 浏览器安装")
        return None

    log(f"发现 {len(candidates)} 个浏览器: {', '.join(c['name'] for c in candidates)}")

    # 按偏好排序（候选名形如 "Edge (Default)"，按浏览器家族前缀匹配）
    if preferred_browser != "auto":
        order = [preferred_browser.lower(), "chrome", "edge", "chromium"]
        seen = set()
        order_unique = []
        for name in order:
            if name not in seen:
                seen.add(name)
                order_unique.append(name)

        def family_rank(candidate):
            name = candidate["name"].lower()
            for index, family in enumerate(order_unique):
                if name.startswith(family):
                    return index
            return 999

        candidates.sort(key=family_rank)

    v20_candidates = []
    for browser in candidates:
        log(f"尝试 {browser['name']}...")
        log("  读取加密密钥...")
        aes_key = _extract_aes_key(browser["local_state"])
        if not aes_key:
            log("  密钥提取失败，跳过")
            continue
        log("  密钥提取成功")

        log(f"  读取 Cookies 数据库...")
        cookies, saw_v20 = _read_cookies_from_db(browser["cookies_db"], aes_key)
        if not cookies:
            if saw_v20:
                log(f"  检测到 v20（App-Bound）加密 cookie，离线无法解密，稍后尝试 CDP")
                v20_candidates.append(browser)
            else:
                log(f"  未找到 {TARGET_DOMAIN} 的 cookie，尝试下一个浏览器")
            continue

        log(f"  找到 {len(cookies)} 个 cookie")
        for c in cookies:
            log(f"    {c['name']}={c['value'][:20]}{'...' if len(c['value'])>20 else ''}")

        # 构建 Cookie 头字符串
        parts = [f"{c['name']}={c['value']}" for c in cookies]
        cookie_str = "; ".join(parts)
        log(f"Cookie 提取成功 ({len(cookie_str)} 字符)")
        return cookie_str

    # 离线解密失败但存在 v20 cookie：走 CDP（让浏览器自己解密）
    for browser in v20_candidates:
        cookie_str = _fetch_via_cdp(browser, log)
        if cookie_str:
            return cookie_str

    log("所有浏览器均未找到有效 cookie")
    return None


def _fetch_via_cdp(browser: dict, log) -> str | None:
    """对一个候选 profile 走 CDP 路径读取明文 cookie。

    需要目标浏览器完全退出才能拿到独立调试端口，因此会先结束其进程。
    """
    try:
        from cdp_cookie import (
            fetch_cookies_via_cdp,
            find_browser_exe,
            is_browser_running,
            kill_browser,
        )
    except ImportError:
        log("  CDP 模块不可用，跳过")
        return None

    family = browser.get("family", "")
    exe = find_browser_exe(family)
    if not exe:
        log(f"  未找到 {browser['name']} 可执行文件，跳过 CDP")
        return None

    log(f"  通过 CDP 读取 {browser['name']} 的明文 cookie...")
    if is_browser_running(family):
        log(f"  {family} 正在运行，先将其完全关闭...")
        kill_browser(family)
        time.sleep(1.5)

    cookies = fetch_cookies_via_cdp(
        exe_path=exe,
        user_data_dir=browser["user_data"],
        profile=browser.get("profile", "Default"),
        target_domain=TARGET_DOMAIN,
        logger=log,
    )
    if not cookies:
        log("  CDP 未读取到目标 cookie")
        return None

    log(f"  CDP 读取成功，找到 {len(cookies)} 个 cookie")
    for c in cookies:
        value = c["value"]
        log(f"    {c['name']}={value[:20]}{'...' if len(value) > 20 else ''}")
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    log(f"Cookie 提取成功 ({len(cookie_str)} 字符)")
    return cookie_str


def load_cookies_into_session(session, preferred_browser: str = "auto") -> bool:
    """从浏览器读取 cookie 并注入到 requests.Session。

    返回:
        True 表示成功加载至少一个 cookie
    """
    cookie_string = find_hdu_cookie(preferred_browser)
    if not cookie_string:
        return False

    from requests.cookies import create_cookie

    loaded = False
    for part in cookie_string.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookie = create_cookie(
            name=name,
            value=value,
            domain=TARGET_DOMAIN,
            path="/",
        )
        session.cookies.set_cookie(cookie)
        loaded = True

    if loaded:
        session.headers["Cookie"] = cookie_string

    return loaded
