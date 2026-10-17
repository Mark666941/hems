"""多核心平行計算（優化 D3）：整月分析的每一天彼此獨立，分給多個 CPU 核心同時計算。

- 程序池建立一次後重複使用（Windows 開新程序要先載入 pandas 等套件，約 1 秒，只有第一次付這個成本）
- 每個工作只帶「最佳化器 + 日期」過去（約 80 KB），結果是當天的推演結果（約 85 KB）
- 平行失敗（例如執行環境不允許開程序）時自動改為逐一計算，結果完全相同
- 環境變數 HEMS_WORKERS 可指定使用幾個核心；1 代表不平行
"""
from __future__ import annotations

import logging
import multiprocessing
import os
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

log = logging.getLogger("hems.parallel")
_POOL: ProcessPoolExecutor | None = None


def _container_limits() -> tuple[int | None, int | None]:
    """Linux 容器（例如 Streamlit Community Cloud）實際可用的 CPU 數與記憶體上限（位元組）；讀不到時回傳 None。
    os.cpu_count() 回報的是整台主機的核心數，照它開子程序可能超過容器的記憶體而被強制結束。"""
    cpus = mem = None
    if hasattr(os, "sched_getaffinity"):
        cpus = len(os.sched_getaffinity(0))
    for path in ("/sys/fs/cgroup/cpu.max",):                 # cgroup v2：「配額 週期」
        try:
            quota, period = Path(path).read_text().split()[:2]
            if quota != "max":
                cpus = max(1, min(cpus or 999, int(int(quota) / int(period))))
        except (OSError, ValueError):
            pass
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            value = Path(path).read_text().strip()
            if value != "max" and int(value) < 1 << 50:
                mem = int(value)
                break
        except (OSError, ValueError):
            pass
    return cpus, mem


def worker_count() -> int:
    env = os.environ.get("HEMS_WORKERS")
    if env:
        return max(1, int(env))
    n = max(1, min(12, (os.cpu_count() or 2) - 1))       # 保留一個核心給介面
    cpus, mem = _container_limits()
    if cpus is not None:
        n = min(n, max(1, cpus - 1))
    if mem is not None:                                   # 每個子程序約需 400 MB（pandas、scipy 等），主程序另留 600 MB
        n = min(n, max(1, (mem - 600 * 2**20) // (400 * 2**20)))
    return n


def _pool() -> ProcessPoolExecutor:
    global _POOL
    if _POOL is None:
        # 一律用 spawn：Linux 預設的 fork 會複製正在執行的 Streamlit 伺服器（多執行緒），可能卡住
        _POOL = ProcessPoolExecutor(max_workers=worker_count(), mp_context=multiprocessing.get_context("spawn"))
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
