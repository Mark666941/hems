"""測試共用設定：所有測試使用暫存的 SQLite 資料庫，不會寫入使用者真正的資料（data/user/hems.db）。"""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="hems_test_"))
os.environ["HEMS_DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
