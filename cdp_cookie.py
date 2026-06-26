"""通过 Chrome DevTools Protocol (CDP) 从浏览器读取明文 cookie。

用途：
    新版 Edge / Chrome 使用 App-Bound Encryption（cookie 值前缀 v20），
    其密钥受 SYSTEM 级 DPAPI 保护，普通进程无法离线解密。
    本模块改为以远程调试模式启动一个 headless 浏览器实例，让浏览器自己
    完成 v20 解密，再通过 CDP 的 Storage.getCookies 拿到明文 cookie。

要点（均为实测结论）：
    - 必须对“真实”的 user-data-dir 开调试；复制 profile 会导致 v20 解密失败。
    - 必须用 Storage.getCookies（浏览器级），Network.getAllCookies 返回空。
    - 目标浏览器必须先完全退出，否则新进程只会附着到已有实例、拿不到调试端口。

只依赖标准库：内置一个最小 WebSocket 客户端，无需第三方包。
"""

import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# 最小 WebSocket 客户端（仅支持文本帧，足够跑 CDP）
# ---------------------------------------------------------------------------

class _WebSocket:
    def __init__(self, url: str, timeout: float = 10.0):
        if not url.startswith("ws://"):
            raise ValueError(f"仅支持 ws:// 地址：{url}")
        host_port, _, path = url[len("ws://"):].partition("/")
        host, _, port = host_port.partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)

        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode())

        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = self.sock.recv(1)
            if not chunk:
                raise ConnectionError("WebSocket 握手失败")
            buffer += chunk
        self._buffer = buffer.split(b"\r\n\r\n", 1)[1]

    def _read(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WebSocket 连接已关闭")
            self._buffer += chunk
        out, self._buffer = self._buffer[:count], self._buffer[count:]
        return out

    def send(self, text: str) -> None:
        data = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + 文本帧
        mask = os.urandom(4)
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> str:
        first, second = self._read(2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read(8))[0]
        # 服务器->客户端不加掩码
        payload = self._read(length)
        return payload.decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 浏览器可执行文件发现
# ---------------------------------------------------------------------------

def find_browser_exe(family: str) -> str | None:
    """根据浏览器家族名（chrome/edge/chromium）返回可执行文件路径。"""
    family = (family or "").lower()
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local_appdata = os.environ.get("LOCALAPPDATA", "")

    table = {
        "edge": [
            Path(program_files_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(program_files) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ],
        "chrome": [
            Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(program_files_x86) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(local_appdata) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ],
        "chromium": [
            Path(local_appdata) / "Chromium" / "Application" / "chrome.exe",
        ],
    }
    for path in table.get(family, []):
        if path.exists():
            return str(path)
    return None


# ---------------------------------------------------------------------------
# 进程探测 / 关闭
# ---------------------------------------------------------------------------

def _image_name(family: str) -> str:
    return "msedge.exe" if family == "edge" else "chrome.exe"


def is_browser_running(family: str) -> bool:
    """检测目标浏览器是否有进程在运行。"""
    image = _image_name(family)
    try:
        out = subprocess.run(
            ["tasklist", "/fi", f"imagename eq {image}", "/nh"],
            capture_output=True, text=True, timeout=5,
        ).stdout.lower()
        return image.lower() in out
    except Exception:
        return False


def kill_browser(family: str) -> None:
    """强制结束目标浏览器的所有进程（含后台“启动加速”进程）。"""
    image = _image_name(family)
    try:
        subprocess.run(
            ["taskkill", "/f", "/im", image, "/t"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------------------
# 核心：通过 CDP 读取明文 cookie
# ---------------------------------------------------------------------------

def fetch_cookies_via_cdp(
    exe_path: str,
    user_data_dir: str,
    profile: str,
    target_domain: str,
    logger=None,
    startup_timeout: float = 12.0,
) -> list[dict] | None:
    """启动 headless 浏览器并通过 CDP 读取目标域名的明文 cookie。

    返回 [{name, value, domain, path, secure}, ...]，失败返回 None。
    调用前应确保目标浏览器已退出（否则拿不到独立调试端口）。
    """
    log = logger or (lambda msg: None)
    port = _free_port()
    args = [
        exe_path,
        "--headless=new",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        f"--profile-directory={profile}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-sync",
        "about:blank",
    ]

    log(f"  启动调试实例（端口 {port}）...")
    proc = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        ws_url = None
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=1
                ) as resp:
                    ws_url = json.loads(resp.read()).get("webSocketDebuggerUrl")
                    if ws_url:
                        break
            except Exception:
                time.sleep(0.2)

        if not ws_url:
            log("  调试端口未就绪（目标浏览器可能仍在运行，请先完全退出）")
            return None

        ws = _WebSocket(ws_url)
        try:
            ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
            result = None
            for _ in range(80):
                message = json.loads(ws.recv())
                if message.get("id") == 1:
                    result = message
                    break
                if message.get("error"):
                    log(f"  CDP 返回错误：{message['error']}")
                    return None
        finally:
            ws.close()

        cookies = (result or {}).get("result", {}).get("cookies", [])
        hits = []
        for cookie in cookies:
            if target_domain in (cookie.get("domain") or ""):
                hits.append({
                    "name": cookie.get("name"),
                    "value": cookie.get("value"),
                    "domain": cookie.get("domain"),
                    "path": cookie.get("path") or "/",
                    "secure": bool(cookie.get("secure")),
                })
        return hits or None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


# ---------------------------------------------------------------------------
# 专用 profile（与用户日常浏览器隔离，工具独占）
# ---------------------------------------------------------------------------
#
# 设计：工具维护一个独立的 user-data-dir（项目目录下 browser_profile/），
# 用户只在这里登录一次图书馆。之后读取 cookie 时对它跑 headless+CDP，
# 由于该 profile 用户平时不用，启停它对用户日常浏览器零影响，也天然支持
# v20（App-Bound）加密（浏览器自己解密）。

# preferred 解析顺序：显式优先 > edge > chrome > chromium
_FAMILY_ORDER = ["edge", "chrome", "chromium"]


def dedicated_profile_dir() -> Path:
    """返回专用 profile 的 user-data-dir 路径（项目目录下）。"""
    return Path(__file__).with_name("browser_profile")


def resolve_browser_exe(preferred: str = "auto") -> tuple[str, str] | None:
    """按偏好找到一个可用浏览器，返回 (exe_path, family)。

    专用 profile 一旦用某个内核创建，之后应一直用同一内核读取
    （v20 密钥与浏览器绑定）。因此首次创建时确定的内核会记录在
    profile 目录里的 .browser 标记文件中，后续优先沿用。
    """
    marker = dedicated_profile_dir() / ".browser"
    if marker.exists():
        try:
            recorded = marker.read_text(encoding="utf-8").strip().lower()
        except OSError:
            recorded = ""
        if recorded:
            exe = find_browser_exe(recorded)
            if exe:
                return exe, recorded

    order = []
    pref = (preferred or "auto").lower()
    if pref in _FAMILY_ORDER:
        order.append(pref)
    for fam in _FAMILY_ORDER:
        if fam not in order:
            order.append(fam)

    for family in order:
        exe = find_browser_exe(family)
        if exe:
            return exe, family
    return None


def _record_browser_family(family: str) -> None:
    profile_dir = dedicated_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        (profile_dir / ".browser").write_text(family, encoding="utf-8")
    except OSError:
        pass


def open_login_window(
    target_url: str,
    preferred: str = "auto",
    logger=None,
) -> bool:
    """用专用 profile 打开一个【可见】浏览器窗口供用户登录。

    非阻塞：启动后立即返回，用户登录完手动关闭窗口即可，cookie 会留在
    专用 profile 里。返回 True 表示窗口已成功启动。
    """
    log = logger or (lambda msg: None)
    resolved = resolve_browser_exe(preferred)
    if not resolved:
        log("未找到可用的浏览器（Chrome / Edge）")
        return False
    exe, family = resolved
    _record_browser_family(family)

    profile_dir = dedicated_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)

    args = [
        exe,
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        target_url,
    ]
    log(f"正在用专用 profile 打开 {family} 登录窗口...")
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log(f"启动浏览器失败：{exc}")
        return False
    log("登录窗口已打开。请在窗口中登录图书馆，登录完成后关闭该窗口。")
    return True


def read_dedicated_cookies(
    target_domain: str,
    preferred: str = "auto",
    logger=None,
) -> list[dict] | None:
    """对专用 profile 跑 headless+CDP 读取目标域名的明文 cookie。

    返回 [{name, value, domain, path, secure}, ...]，无 / 失败返回 None。
    """
    log = logger or (lambda msg: None)
    profile_dir = dedicated_profile_dir()
    if not (profile_dir / "Default").exists() and not (profile_dir / "Local State").exists():
        log("专用 profile 尚未创建（请先点击登录图书馆完成一次登录）")
        return None

    resolved = resolve_browser_exe(preferred)
    if not resolved:
        log("未找到可用的浏览器（Chrome / Edge）")
        return None
    exe, family = resolved

    # 专用 profile 独占使用，正常不会有进程占用；但若用户用同内核手动开过它，
    # headless 实例无法附着，这里不强杀用户的日常浏览器（不同 user-data-dir
    # 的同内核实例可以共存，headless 仍能独立启动）。
    return fetch_cookies_via_cdp(
        exe_path=exe,
        user_data_dir=str(profile_dir),
        profile="Default",
        target_domain=target_domain,
        logger=log,
    )
