"""在 Windows 桌面建立「HEMS 居家能源管理系統」捷徑，雙擊即開啟桌面視窗版。

用法：python scripts/create_shortcut.py
捷徑以 pythonw（不顯示命令視窗）執行 run_app.py，使用 assets/hems.ico 圖示。
專案資料夾搬家後，重新執行一次即可。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "HEMS 居家能源管理系統"


def ps_quote(text: str) -> str:
    """PowerShell 單引號字串（單引號要重複兩次）。"""
    return "'" + str(text).replace("'", "''") + "'"


def main() -> None:
    if sys.platform != "win32":
        sys.exit("這個腳本只支援 Windows。")
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    icon = ROOT / "assets" / "hems.ico"
    for path in (pythonw, icon):
        if not path.exists():
            sys.exit(f"找不到 {path}（請先建立虛擬環境、執行 python scripts/make_icon.py）")

    # 用 Windows 內建的 WScript.Shell 建立 .lnk 捷徑；桌面位置由系統查詢（OneDrive 同步桌面也能找到）
    script = f"""
$desktop = [Environment]::GetFolderPath('Desktop')
$path = Join-Path $desktop {ps_quote(NAME + '.lnk')}
$link = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
$link.TargetPath = {ps_quote(pythonw)}
$link.Arguments = '"' + {ps_quote(ROOT / 'run_app.py')} + '"'
$link.WorkingDirectory = {ps_quote(ROOT)}
$link.IconLocation = {ps_quote(icon)}
$link.Description = '居家能源管理系統：模擬、最佳化與模式比較'
$link.Save()
Write-Output $path
"""
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        sys.exit(f"建立捷徑失敗：{result.stderr}")
    print(f"已建立桌面捷徑：{result.stdout.strip()}")


if __name__ == "__main__":
    main()
