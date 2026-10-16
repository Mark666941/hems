"""第 6a 批：整月分析折線圖（每日用電、累積電費、溫度與冷氣用電）與數據分析式建議。"""
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from ui import charts, insights
from ui.settings import build_analyzer, default_settings

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture(scope="module")
def daily():
    """7 月前 14 天：基準與省電模式。"""
    a = build_analyzer(default_settings())
    days = [date(2026, 7, d) for d in range(1, 15)]
    scenarios = {insights.BASELINE: [a.sim.run(d, "tou_2", a.opt.baseline_plan) for d in days],
                 "省電模式": [a.opt.optimize(d, "saving", "tou_2").result for d in days]}
    return insights.daily_table(scenarios)


def test_daily_table_and_cumulative_cost(daily):
    assert len(daily) == 28 and set(daily["模式"]) == {insights.BASELINE, "省電模式"}
    for _, g in daily.groupby("模式"):
        assert g["cum_cost"].iloc[-1] == pytest.approx(g["cost"].sum())
        assert g["cum_cost"].is_monotonic_increasing
    assert (daily["ac_kwh"] <= daily["kwh"]).all()


def test_temperature_slope():
    df = pd.DataFrame({"temp_mean_c": [26, 27, 28, 29, 30, 31], "ac_kwh": [2, 3, 4, 5, 6, 7]})
    slope, intercept, r2 = insights.temperature_slope(df)
    assert slope == pytest.approx(1.0) and intercept == pytest.approx(-24.0) and r2 == pytest.approx(1.0)
    flat = pd.DataFrame({"temp_mean_c": [20.0] * 6, "ac_kwh": [1.0] * 6})
    assert insights.temperature_slope(flat) is None        # 溫度沒有變化，無法迴歸


def test_month_insights_have_numbers(daily):
    found = insights.month_insights(daily, "省電模式", 3.0)
    titles = " ".join(i.title for i in found)
    assert "用電最多的" in titles
    assert any("省電模式整月可省" in i.title for i in found)      # 省電模式比基準便宜
    assert all(any(ch.isdigit() for ch in i.html()) for i in found)  # 每則建議都有數字根據


def test_month_charts_build(daily):
    for chart in (charts.month_daily_lines(daily), charts.month_daily_lines(daily, "cum_cost", "累積電費（元）"),
                  charts.temp_vs_ac(daily)):
        assert chart.to_dict()


def test_month_analysis_page():
    at = AppTest.from_file(APP, default_timeout=300)
    at.session_state["_test_page"] = "analysis"
    at.run()
    next(b for b in at.button if b.label == "執行整月分析").click().run()
    assert not at.exception and not at.error
    md = " ".join(m.value for m in at.markdown)
    assert "各模式每日用電" in md and "累積流動電費" in md and "室外溫度與冷氣用電" in md
    assert "冷氣每天約多用" in md                                  # 夏季：溫度敏感度建議
    assert any(s.value == "數據分析" for s in at.subheader)
