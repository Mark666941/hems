"""多核心平行計算（優化 D3）：整月分析的每一天彼此獨立，分給多個 CPU 核心同時計算。

- 程序池建立一次後重複使用（Windows 開新程序要先載入 pandas 等套件，約 1 秒，只有第一次付這個成本）
- 每個工作只帶「最佳化器 + 日期」過去（約 80 KB），結果是當天的推演結果（約 85 KB）
- 平行失敗（例如執行環境不允許開程序）時自動改為逐一計算，結果完全相同
- 環境變數 HEMS_WORKERS 可指定使用幾個核心；1 代表不平行
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor

log = logging.getLogger("hems.parallel")
_POOL: ProcessPoolExecutor | None = None


def worker_count() -> int:
    env = os.environ.get("HEMS_WORKERS")
    if env:
        return max(1, int(env))
    return max(1, min(12, (os.cpu_count() or 2) - 1))   # 保留一個核心給介面


def _pool() -> ProcessPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = ProcessPoolExecutor(max_workers=worker_count())
    return _POOL


def parallel_map(fn: Callable, items, *args) -> list:
    """回傳 [fn(item, *args) for item in items]，依 items 順序。fn 必須是模組層級的函式（才能傳給其他程序）。"""
    global _POOL
    items = list(items)
    if worker_count() <= 1 or len(items) <= 1:
        return [fn(item, *args) for item in items]
    try:
        futures = [_pool().submit(fn, item, *args) for item in items]
        return [f.result() for f in futures]
    except Exception as e:   # noqa: BLE001
        # 無法傳給其他程序（Python 3.11 序列化區域函式丟 AttributeError、3.14 丟 PicklingError）、
        # 程序池損壞、環境不允許開程序等：改為逐一計算。若是計算本身的錯誤，逐一計算時會再次發生並往上丟出。
        log.warning("平行計算失敗，改為逐一計算：%s: %s", type(e).__name__, e)
        if _POOL is not None:
            _POOL.shutdown(wait=False, cancel_futures=True)
        _POOL = None
        return [fn(item, *args) for item in items]


def shutdown() -> None:
    global _POOL
    if _POOL is not None:
        _POOL.shutdown(wait=False, cancel_futures=True)
        _POOL = None
