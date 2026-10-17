"""D3：多核心平行計算與單核心結果相同；平行失敗時自動退回逐一計算。"""
import pandas as pd
import pytest

from core import parallel
from core.metrics import Analyzer


def _square(x, offset):
    return x * x + offset


def test_parallel_map_keeps_order():
    assert parallel.parallel_map(_square, range(20), 1) == [x * x + 1 for x in range(20)]


def test_falls_back_when_not_picklable():
    # lambda 不能傳給其他程序：自動改為逐一計算，結果仍正確
    assert parallel.parallel_map(lambda x, k: x + k, [1, 2, 3], 10) == [11, 12, 13]


def test_month_parallel_equals_sequential(monkeypatch):
    an = Analyzer()
    fast, _ = an.opt.compare_month(2026, 3)
    monkeypatch.setenv("HEMS_WORKERS", "1")
    slow, _ = an.opt.compare_month(2026, 3)
    pd.testing.assert_frame_equal(fast, slow)


@pytest.mark.parametrize("plan", ["tou_2", "tou_3"])
def test_energy_cost_matches_reprice(plan):
    an = Analyzer()
    r = an.sim.run(pd.Timestamp("2026-07-20").date())
    assert an.sim.energy_cost(r, plan) == pytest.approx(an.sim.reprice(r, plan).energy_cost)


def test_worker_count_respects_container_memory(monkeypatch):
    monkeypatch.delenv("HEMS_WORKERS", raising=False)
    monkeypatch.setattr(parallel, "_container_limits", lambda: (8, 1024 * 2**20))      # 1 GB 容器
    assert parallel.worker_count() == 1                                                  # 記憶體只夠主程序＋1 個
    monkeypatch.setattr(parallel, "_container_limits", lambda: (2, None))
    assert parallel.worker_count() == 1                                                  # 2 核心：保留 1 個給介面
    monkeypatch.setattr(parallel, "_container_limits", lambda: (None, None))
    assert parallel.worker_count() >= 1


def test_postgres_connection_is_encrypted(monkeypatch):
    from core.storage import connect_args
    assert connect_args("sqlite:///x.db") == {}
    monkeypatch.delenv("HEMS_DB_SSL", raising=False)
    assert "ssl_context" in connect_args("postgresql+pg8000://u:p@db.example.com:5432/postgres")
    monkeypatch.setenv("HEMS_DB_SSL", "disable")
    assert connect_args("postgresql+pg8000://u:p@localhost/postgres") == {}


def test_workers_do_not_rerun_streamlit_app(monkeypatch):
    # 在 Streamlit 裡開子程序時，主程式（app.py）暫時換成空模組，子程序不會把整個網頁程式重跑一遍
    import sys

    from streamlit import runtime
    real_main = sys.modules["__main__"]
    monkeypatch.setattr(runtime, "exists", lambda: True)
    with parallel._plain_main():
        assert sys.modules["__main__"] is not real_main and not hasattr(sys.modules["__main__"], "__file__")
    assert sys.modules["__main__"] is real_main                      # 結束後還原
    monkeypatch.setattr(runtime, "exists", lambda: False)
    with parallel._plain_main():
        assert sys.modules["__main__"] is real_main                  # 命令列腳本不受影響
