"""錯誤處理與記錄（D4）。

- get_logger()：整個程式共用的記錄器。寫入 data/user/logs/hems.log（每檔 1 MB、保留 3 份，不進 git）；
  可用環境變數 HEMS_LOG_DIR 指定資料夾；資料夾不能寫入時（例如唯讀的雲端環境）改為只輸出到終端機。
- guarded()：執行一個頁面或區塊，發生未預期的錯誤時記錄完整 traceback，畫面只顯示友善的錯誤訊息與
  「技術細節」，其他頁面與左側選單照常可用，不會整個程式當掉。
  st.rerun()、st.stop() 用的是 BaseException，不會被這裡攔截。
"""
from __future__ import annotations

import logging
import os
import traceback
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

import streamlit as st

DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "user" / "logs"
LOGGER_NAME = "hems"
FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def log_path() -> Path:
    return Path(os.environ.get("HEMS_LOG_DIR") or DEFAULT_LOG_DIR) / "hems.log"


def get_logger(name: str | None = None) -> logging.Logger:
    """取得記錄器（第一次呼叫時設定輸出位置）。name 為子模組名稱，例如 get_logger("storage")。"""
    root = logging.getLogger(LOGGER_NAME)
    if not root.handlers:
        root.setLevel(logging.INFO)
        root.propagate = False
        try:
            path = log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            handler: logging.Handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        except OSError:
            handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(handler)
    return root.getChild(name) if name else root


def show_error(where: str, error: BaseException) -> None:
    """畫面上的錯誤訊息：說明在哪裡出錯、可以怎麼做，技術細節收在展開區塊。"""
    st.error(f"「{where}」發生錯誤：{error}。其他頁面仍可使用；可以重新整理頁面，"
             "或到左側「👤 帳號與設定檔」切換設定檔、在「設定」頁恢復預設值。", icon="⚠️")
    with st.expander("技術細節（回報問題時請附上）"):
        st.code("".join(traceback.format_exception(error)), language="text")
        st.caption(f"完整記錄：{log_path()}")


def guarded(where: str, fn: Callable[[], None]) -> bool:
    """執行 fn；發生錯誤時記錄並顯示錯誤訊息。回傳是否成功。"""
    try:
        fn()
        return True
    except Exception as e:     # 頁面中任何未預期的錯誤都要記錄，不讓整個程式當掉
        get_logger("ui").exception("%s 發生錯誤", where)
        show_error(where, e)
        return False
