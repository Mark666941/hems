"""階段 7：指標與模式比較。"""
from datetime import date

import pytest

from core.metrics import BASELINE, Analyzer, indoor_rh, markdown_table, pmv_ppd

SUMMER = date(2026, 7, 20)


@pytest.fixture(scope="module")
def analyzer():
    return Analyzer()


@pytest.fixture(scope="module")
def day(analyzer):
    return analyzer.compare_day(SUMMER)


# ---------- PMV / PPD ----------

def test_pmv_matches_iso7730_example():
    # ISO 7730 附錄 D 範例：22°C、風速 0.1、RH 60%、1.2 met、0.5 clo → PMV ≈ −0.75、PPD ≈ 17%
    pmv, ppd = pmv_ppd(22, 22, 0.1, 60, 1.2, 0.5)
    assert pmv == pytest.approx(-0.75, abs=0.05)
    assert ppd == pytest.approx(17, abs=1.5)


def test_pmv_neutral_has_min_ppd():
    # PMV = 0 時 PPD 最低為 5%
    pmv, ppd = pmv_ppd(26.5, 26.5, 0.15, 55, 1.0, 0.5)
    assert abs(pmv) < 0.3
    assert 5 <= ppd < 7


def test_pmv_trends():
    cold, _ = pmv_ppd(24, 24, 0.15, 55, 1.0, 0.5)
    hot, _ = pmv_ppd(29, 29, 0.15, 55, 1.0, 0.5)
    assert cold < 0 < hot
    warm_clothes, _ = pmv_ppd(24, 24, 0.15, 55, 1.0, 1.0)
    assert warm_clothes > cold   # 穿多一點比較不冷


def test_indoor_rh_from_outdoor():
    # 室外 30°C / 70%，室內較冷時相對濕度升高、較熱時降低
    assert indoor_rh(28, 30, 70) > 70
    assert indoor_rh(32, 30, 70) < 70
    assert indoor_rh(15, 30, 90) == 100   # 不超過 100%


# ---------- 單日指標 ----------

def test_day_metrics_columns_and_ranges(day):
    table, _ = day
    assert list(table["模式"]) == [BASELINE, "省電模式", "正常模式", "效能模式", "舒眠模式"]
    for col in ("尖峰占比(%)", "負載率(%)", "舒適時間比例(%)", "PMV舒適比例(%)"):
        assert table[col].between(0, 100).all()
    assert (table["平均PPD(%)"] >= 5).all()          # PPD 最低 5%
    assert table.loc[0, "電費節省(%)"] == 0


def test_carbon_proportional_to_energy(analyzer, day):
    table, _ = day
    factor = analyzer.cfg["emission"]["kg_co2e_per_kwh"]
    assert (table["碳排放(kg)"] - table["用電(kWh)"] * factor).abs().max() < 1e-9


def test_two_comfort_metrics_agree_baseline_overcools(day):
    # 溫度範圍指標與 PMV 都判定：基準 25°C 偏冷、正常模式較舒適
    table, _ = day
    base, normal = table.iloc[0], table[table["模式"] == "正常模式"].iloc[0]
    assert normal["舒適時間比例(%)"] > base["舒適時間比例(%)"]
    assert normal["PMV舒適比例(%)"] > base["PMV舒適比例(%)"]
    assert normal["平均PPD(%)"] < base["平均PPD(%)"]


def test_shifting_lowers_peak_share_and_demand(day):
    table, _ = day
    base, normal = table.iloc[0], table[table["模式"] == "正常模式"].iloc[0]
    assert normal["尖峰占比(%)"] < base["尖峰占比(%)"]
    assert normal["最大需量(kW)"] < base["最大需量(kW)"]
    assert normal["負載率(%)"] > base["負載率(%)"]     # 用電更平穩


def test_cost_by_category_sums_to_energy_cost(day):
    _, results = day
    r = results["基準"]
    assert r.cost_by_category().sum() == pytest.approx(r.energy_cost)


# ---------- 整月與建議 ----------

@pytest.fixture(scope="module")
def month(analyzer):
    table, scenarios = analyzer.compare_month(2026, 7, "tou_2")
    return table, scenarios, analyzer.recommendations(2026, 7, "tou_2", table, scenarios)


def test_month_table(month):
    table, scenarios, _ = month
    assert len(table) == 5 and len(scenarios[BASELINE]) == 31
    assert table.loc[0, "月電費節省(%)"] == 0
    assert (table.loc[1:, "月電費節省(%)"] > 0).all()


def test_recommendations(month):
    table, _, recs = month
    assert recs[0].title == "綜合建議"
    assert "請勿直接相加" in recs[0].note
    titles = [r.title for r in recs]
    assert "改用正常模式" in titles
    assert "冷氣調高 1°C" in titles
    # 綜合建議的節省 = 基準月電費 − 推薦組合月電費，且不小於任何單項
    assert recs[0].monthly_saving >= max(r.monthly_saving for r in recs[1:])
    # 除綜合建議外，依每月節省排序
    savings = [r.monthly_saving for r in recs[1:]]
    assert savings == sorted(savings, reverse=True)


def test_markdown_table():
    import pandas as pd
    text = markdown_table(pd.DataFrame({"a": [1, 2], "b": ["x", "y"]}))
    assert text.splitlines() == ["| a | b |", "|---|---|", "| 1 | x |", "| 2 | y |"]
