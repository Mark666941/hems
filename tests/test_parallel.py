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
