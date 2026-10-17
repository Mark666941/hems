"""一鍵啟動 HEMS 介面。

用法：
    python run_app.py            # 桌面視窗（需要 pywebview；沒有安裝時自動改用瀏覽器）
    python run_app.py --browser  # 用瀏覽器開啟
    pythonw run_app.py           # 桌面捷徑用：不顯示命令視窗

在 VS Code：按 F5，選「HEMS 介面（桌面視窗）」或「HEMS 介面（瀏覽器）」。
不開 VS Code：雙擊 start_hems.bat，或桌面捷徑（python scripts/create_shortcut.py 建立）。

做法：在背景啟動 Streamlit 伺服器（自動找一個沒被佔用的連接埠、只接受本機連線），
桌面視窗先顯示「啟動中」畫面，伺服器就緒後切換到介面；關閉視窗時一併關閉伺服器。
沒有命令視窗時（pythonw），伺服器訊息寫到 output/hems_app.log，啟動失敗會跳出錯誤對話框。
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

TITLE = "HEMS 居家能源管理系統"
FROZEN = getattr(sys, "frozen", False)  # 由 PyInstaller 打包成 HEMS.exe（Windows 安裝程式，E3）
if FROZEN:
    # 程式檔在安裝資料夾（一般使用者不能寫入），使用者資料放 %LOCALAPPDATA%\HEMS：
    # 資料庫、錯誤記錄、Open-Meteo 天氣快取、伺服器訊息。用 setdefault 讓測試可以另外指定。
    ROOT = Path(sys._MEIPASS)
    USER_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HEMS"
    USER_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HEMS_DATABASE_URL", f"sqlite:///{(USER_DIR / 'hems.db').as_posix()}")
    os.environ.setdefault("HEMS_LOG_DIR", str(USER_DIR / "logs"))
    os.environ.setdefault("HEMS_WEATHER_CACHE", str(USER_DIR / "weather"))
    LOG_PATH = USER_DIR / "hems_app.log"
else:
    ROOT = Path(__file__).resolve().parent
    LOG_PATH = ROOT / "output" / "hems_app.log"
HAS_CONSOLE = sys.stdout is not None   # pythonw 或打包的視窗程式沒有命令視窗，stdout 為 None

LOADING_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
font-family:"Microsoft JhengHei",system-ui,sans-serif;background:#fcfcfb;color:#52514e}
.dot{width:12px;height:12px;border-radius:50%;background:#2a78d6;display:inline-block;margin:0 5px;
animation:b 1.2s infinite ease-in-out}.dot:nth-child(2){animation-delay:.2s}.dot:nth-child(3){animation-delay:.4s}
@keyframes b{0%,80%,100%{opacity:.25}40%{opacity:1}}h1{font-size:22px;color:#0b0b0b;margin:0 0 6px}
p{margin:0 0 24px;font-size:14px}</style></head><body>
<h1>⚡ HEMS 居家能源管理系統</h1><p>正在啟動模擬引擎，約需 5–10 秒…</p>
<div><span class="dot"></span><span class="dot"></span><span class="dot"></span></div></body></html>"""


def free_port() -> int:
    """請作業系統分配一個目前沒人使用的連接埠，避免和已經開著的伺服器衝突。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until_ready(url: str, proc: subprocess.Popen, timeout_s: float = 90) -> bool:
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


def show_error(message: str) -> None:
    """沒有命令視窗時用 Windows 對話框顯示錯誤；有命令視窗時直接印出。"""
    print(message)
    if not HAS_CONSOLE and sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x10)


def start_server(port: int) -> subprocess.Popen:
    output = None
    if not HAS_CONSOLE:                      # 沒有命令視窗：伺服器訊息寫入記錄檔
        LOG_PATH.parent.mkdir(exist_ok=True)
        output = open(LOG_PATH, "w", encoding="utf-8")   # noqa: SIM115 伺服器執行期間都要寫入，不能用 with 關閉
    # 打包版沒有 python 指令可用：用 HEMS.exe 自己加上 --serve 參數開出伺服器程序
    command = [sys.executable, "--serve", str(port)] if FROZEN else         [sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py"), *streamlit_flags(port)]
    return subprocess.Popen(
        command,
        cwd=ROOT, stdout=output, stderr=subprocess.STDOUT if output else None,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if not HAS_CONSOLE else 0,
    )


def streamlit_flags(port: int) -> list[str]:
    return ["--server.port", str(port), "--server.address", "localhost",   # 只接受本機連線
            "--server.headless", "true", "--browser.gatherUsageStats", "false"]


def serve(port: int) -> None:
    """打包版的伺服器程序（HEMS.exe --serve 連接埠）：在這個程序內執行 Streamlit。"""
    if sys.stdout is None:                   # 視窗程式沒有標準輸出：Streamlit 的訊息寫到記錄檔
        sys.stdout = sys.stderr = open(LOG_PATH, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    os.chdir(ROOT)                           # 讀取 .streamlit/config.toml（主題）
    from streamlit.web import cli as stcli
    sys.argv = ["streamlit", "run", str(ROOT / "app.py"), *streamlit_flags(port),
                "--global.developmentMode", "false"]   # 打包後 Streamlit 會誤判為開發模式
    sys.exit(stcli.main())


def stop_server(server: subprocess.Popen) -> None:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()


def run_window(url: str, server: subprocess.Popen) -> None:
    """桌面視窗：先顯示啟動畫面，伺服器就緒後切換到介面。"""
    import webview  # pywebview：用 Windows 內建的 Edge 核心顯示網頁

    window = webview.create_window(TITLE, html=LOADING_HTML, width=1440, height=920, min_size=(960, 640))

    def load_when_ready() -> None:
        if wait_until_ready(url, server):
            window.load_url(url)
        else:
            show_error(f"伺服器啟動失敗，詳細訊息請看：{LOG_PATH if not HAS_CONSOLE else '命令視窗'}")
            window.destroy()

    threading.Thread(target=load_when_ready, daemon=True).start()
    webview.start()                          # 關閉視窗後才會往下執行


def main() -> None:
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--browser", action="store_true", help="用瀏覽器開啟，而不是桌面視窗")
    parser.add_argument("--serve", type=int, metavar="PORT", help=argparse.SUPPRESS)   # 打包版內部使用
    args = parser.parse_args()
    if args.serve:
        serve(args.serve)
        return

    port = free_port()
    url = f"http://localhost:{port}"
    print(f"啟動 {TITLE}（{url}）…")
    server = start_server(port)
    try:
        use_window = not args.browser
        if use_window:
            try:
                import webview  # noqa: F401
            except ImportError:
                print("沒有安裝 pywebview，改用瀏覽器開啟（安裝：pip install -r requirements-desktop.txt）")
                use_window = False

        if use_window:
            run_window(url, server)
        elif wait_until_ready(url, server):
            webbrowser.open(url)
            print("已在瀏覽器開啟。按 Ctrl+C 結束。")
            server.wait()
        else:
            show_error("伺服器啟動失敗，請查看上方的錯誤訊息。")
    except KeyboardInterrupt:
        pass
    finally:
        stop_server(server)
        print("已關閉。")


if __name__ == "__main__":
    multiprocessing.freeze_support()         # 打包版：多核心平行計算的子程序由這裡接手，不會再開一個視窗
    main()
