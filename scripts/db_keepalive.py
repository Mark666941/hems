"""保持雲端資料庫活躍：Supabase 免費方案 7 天沒有資料庫活動會暫停專案，網頁版就無法登入。

由 GitHub Actions（.github/workflows/keepalive.yml）每 3 天執行一次：連到資料庫查詢帳號數，算一次真實的資料庫活動。
資料庫網址放在 GitHub repository 的 Secrets（名稱 HEMS_DATABASE_URL），不會出現在程式碼或執行紀錄裡。

本機手動測試：
    set HEMS_DATABASE_URL=postgresql+pg8000://…   （Windows 命令提示字元）
    python scripts/db_keepalive.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from core.storage import connect_args  # noqa: E402  只依賴 sqlalchemy，不需要安裝整個專案


def main() -> None:
    url = os.environ.get("HEMS_DATABASE_URL", "").strip()
    if not url:
        print("沒有設定 HEMS_DATABASE_URL（GitHub → Settings → Secrets and variables → Actions），略過。")
        return
    engine = create_engine(url, connect_args=connect_args(url))
    with engine.connect() as conn:
        users = conn.execute(text("select count(*) from users")).scalar()
        profiles = conn.execute(text("select count(*) from profiles")).scalar()
    print(f"資料庫連線正常：{users} 個帳號、{profiles} 組設定檔")


if __name__ == "__main__":
    main()
