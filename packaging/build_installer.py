"""一鍵製作 Windows 安裝程式（優化 E3）。

用法：python packaging/build_installer.py [版本，預設 1.0.0]
步驟：
1. PyInstaller 依 packaging/hems.spec 打包成 build/dist/HEMS/HEMS.exe（約 300 MB 的資料夾）
2. Inno Setup（ISCC.exe）依 packaging/hems_installer.iss 包成 build/installer/HEMS-Setup-版本.exe

需要：pip install pyinstaller；Inno Setup 6（winget install JRSoftware.InnoSetup）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ISCC_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
]


def main() -> None:
    version = sys.argv[1] if len(sys.argv) > 1 else "1.0.0"
    print("1/2 PyInstaller 打包中（約 2 分鐘）…")
    subprocess.run([sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "hems.spec"), "--noconfirm",
                    "--distpath", str(ROOT / "build" / "dist"), "--workpath", str(ROOT / "build" / "work")],
                   check=True, cwd=ROOT)
    iscc = next((p for p in ISCC_CANDIDATES if p.exists()), None) or shutil.which("ISCC")
    if iscc is None:
        sys.exit("找不到 Inno Setup（ISCC.exe）。請先安裝：winget install JRSoftware.InnoSetup")
    print("2/2 Inno Setup 製作安裝精靈中（約 3 分鐘）…")
    subprocess.run([str(iscc), f"/DAppVersion={version}", str(ROOT / "packaging" / "hems_installer.iss")],
                   check=True, cwd=ROOT / "packaging")
    out = ROOT / "build" / "installer" / f"HEMS-Setup-{version}.exe"
    print(f"完成：{out}（{out.stat().st_size / 1e6:.0f} MB）")


if __name__ == "__main__":
    main()
