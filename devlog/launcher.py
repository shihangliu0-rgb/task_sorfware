"""启动器：拉起本地服务，并用「应用窗口」模式打开界面。

优先用 Chrome / Edge 的 --app 模式（没有地址栏，看起来就是个原生桌面软件），
找不到浏览器时退回默认浏览器打开。
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from .db import data_home
from .server import run

CHROME_CANDIDATES = {
    "Windows": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "Darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ],
    "Linux": [],
}
LINUX_BINS = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
              "microsoft-edge", "brave-browser"]


def find_browser() -> str | None:
    system = platform.system()
    for p in CHROME_CANDIDATES.get(system, []):
        if Path(p).exists():
            return p
    for b in LINUX_BINS:
        found = shutil.which(b)
        if found:
            return found
    return None


def free_port(preferred: int = 8765) -> int:
    for port in range(preferred, preferred + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred


def wait_ready(url: str, timeout: float = 12.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.15)
    return False


def open_window(url: str) -> subprocess.Popen | None:
    browser = find_browser()
    if not browser:
        import webbrowser
        webbrowser.open(url)
        return None
    profile = Path(tempfile.gettempdir()) / "devlog-profile"
    args = [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--window-size=1360,880",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,ChromeWhatsNewUI",
    ]
    try:
        return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        import webbrowser
        webbrowser.open(url)
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="devlog", description="DevLog 开发工作日志")
    ap.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    ap.add_argument("--no-window", action="store_true", help="只启动服务，不打开窗口")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)

    port = free_port(args.port)
    url = f"http://{args.host}:{port}/"

    say = lambda m: print(m, flush=True)
    say("┌────────────────────────────────────────┐")
    say("│  DevLog · 开发工作日志                 │")
    say("└────────────────────────────────────────┘")
    say(f"  数据目录：{data_home()}")
    say(f"  服务地址：{url}")

    httpd = run(host=args.host, port=port, block=False)

    if not wait_ready(url + "api/ping"):
        print("  ⚠️ 服务启动超时", file=sys.stderr)
        return 1

    if args.no_window:
        say("  已启动（--no-window），按 Ctrl+C 退出")
    else:
        proc = open_window(url)
        say("  窗口已打开。关闭窗口后回到这里按 Ctrl+C 退出。")
        if proc:
            try:
                proc.wait()
                say("\n  窗口已关闭，正在退出…")
                httpd.shutdown()
                return 0
            except KeyboardInterrupt:
                pass

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        say("\n  已退出。")
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
