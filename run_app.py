"""一鍵啟動 HEMS 介面。

用法：
    python run_app.py            # 桌面視窗（需要 pywebview；沒有安裝時自動改用瀏覽器）
    python run_app.py --browser  # 用瀏覽器開啟

在 VS Code：按 F5，選「HEMS 介面（桌面視窗）」或「HEMS 介面（瀏覽器）」。
不開 VS Code：雙擊 start_hems.bat。

做法：在背景啟動 Streamlit 伺服器（自動找一個沒被佔用的連接埠），等伺服器就緒後，
用桌面視窗或瀏覽器開啟；關閉視窗（或在瀏覽器模式按 Ctrl+C）時一併關閉伺服器。
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TITLE = "HEMS 居家能源管理系統"


def free_port() -> int:
    """請作業系統分配一個目前沒人使用的連接埠，避免和已經開著的伺服器衝突。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until_ready(url: str, proc: subprocess.Popen, timeout_s: float = 60) -> bool:
    """輪詢 Streamlit 的健康檢查網址，直到伺服器可以連線。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:          # 伺服器啟動失敗、已經結束
            return False
        try:
            with urllib.request.urlopen(f"{url}/_stcore/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            time.sleep(0.5)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--browser", action="store_true", help="用瀏覽器開啟，而不是桌面視窗")
    args = parser.parse_args()

    port = free_port()
    url = f"http://localhost:{port}"
    print(f"啟動 {TITLE}（{url}）…")
    server = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py"),
         "--server.port", str(port), "--server.headless", "true",
         "--browser.gatherUsageStats", "false"],
        cwd=ROOT,
    )
    try:
        if not wait_until_ready(url, server):
            print("伺服器啟動失敗，請查看上方的錯誤訊息。")
            return

        use_window = not args.browser
        if use_window:
            try:
                import webview  # pywebview：用 Windows 內建的 Edge 核心顯示網頁
            except ImportError:
                print("沒有安裝 pywebview，改用瀏覽器開啟（安裝：pip install -r requirements-desktop.txt）")
                use_window = False

        if use_window:
            webview.create_window(TITLE, url, width=1440, height=920, min_size=(960, 640))
            webview.start()                  # 關閉視窗後才會往下執行
        else:
            webbrowser.open(url)
            print("已在瀏覽器開啟。按 Ctrl+C 結束。")
            server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        print("已關閉。")


if __name__ == "__main__":
    main()
