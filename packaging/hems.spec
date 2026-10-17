# PyInstaller 打包設定（優化 E3：Windows 安裝程式）
# 用法：python -m PyInstaller packaging/hems.spec --noconfirm   → dist/HEMS/HEMS.exe
# 之後用 Inno Setup 把 dist/HEMS 包成安裝精靈（packaging/hems_installer.iss）。
#
# 重點：
# - 入口是 run_app.py（桌面視窗）；app.py 由 Streamlit 在執行時讀取，所以當作資料檔放進去，
#   它匯入的 core/、ui/ 用 collect_submodules 全部收進來
# - Streamlit 需要自己的網頁靜態檔與套件中繼資料（版本檢查用）
# - scikit-learn 只有命令列的機器學習腳本會用，介面不需要，排除以縮小安裝檔
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))

datas = [
    (str(ROOT / "app.py"), "."),
    (str(ROOT / "config"), "config"),
    (str(ROOT / "data" / "weather"), "data/weather"),
    (str(ROOT / "data" / "meter"), "data/meter"),
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "ui" / "floorplan_frontend"), "ui/floorplan_frontend"),
    (str(ROOT / ".streamlit" / "config.toml"), ".streamlit"),
]
datas += collect_data_files("streamlit") + copy_metadata("streamlit")
for pkg in ("altair", "vega_datasets", "pyarrow"):
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass
for pkg in ("altair", "pandas", "numpy", "pyarrow", "sqlalchemy", "scipy", "matplotlib", "pywebview"):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass

hiddenimports = (collect_submodules("core") + collect_submodules("ui") + collect_submodules("streamlit")
                 + ["scipy.optimize", "scipy.sparse", "matplotlib.backends.backend_pdf", "matplotlib.backends.backend_agg",
                    "sqlalchemy.dialects.sqlite", "webview"])
# 排除測試檔被當成子模組收進來
hiddenimports = [m for m in hiddenimports if ".tests" not in m and not m.startswith("streamlit.testing")]

a = Analysis(
    [str(ROOT / "run_app.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["sklearn", "pytest", "IPython", "notebook", "tkinter", "PyQt5", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="HEMS",
    console=False,                     # 視窗程式，不顯示命令視窗
    icon=str(ROOT / "assets" / "hems.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="HEMS")
