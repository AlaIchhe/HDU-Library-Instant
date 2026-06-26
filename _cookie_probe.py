"""隔离探针：在子进程中运行 find_hdu_cookie，防止 native crash 拖垮主进程。

进度日志输出到 stderr，最终 JSON 结果输出到 stdout。
由 _safe_find_hdu_cookie() 通过 subprocess 调用。
"""

import json
import sys
from datetime import datetime

from cookie_browser import find_hdu_cookie


def log(message: str):
    """输出带时间戳的进度日志到 stderr。"""
    stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{stamp}] {message}"
    print(line, file=sys.stderr, flush=True)


def main():
    preferred = sys.argv[1] if len(sys.argv) > 1 else "auto"

    log(f"探针启动，目标浏览器: {preferred}")

    try:
        cookie = find_hdu_cookie(preferred, logger=log)
    except Exception as exc:
        log(f"异常: {exc}")
        cookie = None

    if cookie:
        log(f"成功获取 cookie，长度 {len(cookie)} 字符")
    else:
        log("未获取到 cookie")

    # 最终 JSON 结果输出到 stdout（唯一的一行 JSON）
    result = {"cookie": cookie}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
