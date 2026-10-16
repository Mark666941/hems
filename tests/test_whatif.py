"""C4：情境比較（What-if）與投資回收年限。"""
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from core.data_source import CwaCsvWeatherSource
from ui import whatif
from ui.settings import build_house, default_settings

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def results():
    s, p = default_settings(), whatif.default_params()
    available = set(CwaCsvWeatherSource().available_dates())
    out = {}
    for sid in ["current"] + [x.id for x in whatif.SCENARIOS]:
        ss, pp = whatif.apply_scenario(s, p, sid)
        out[sid] = whatif.annual(ss, pp, "tou_2", available)
    return out


def test_apply_scenario_changes_only_one_thing():
    s, p = default_settings(), whatif.default_params()
    _, p2 = whatif.apply_scenario(s, p, "ac_upgrade")
    assert p2["air_conditioner"]["rated_cop"] == 4.5 and p["air_conditioner"]["rated_cop"] == 3.6
    s2, _ = whatif.apply_scenario(s, p, "led")
    lights = {f"{sp.id}/{a.id}": a.power_w for sp, a in build_house(s).all_appliances() if a.id == "lighting"}
    after = {f"{sp.id}/{a.id}": a.power_w for sp, a in build_house(s2).all_appliances() if a.id == "lighting"}
    assert all(after[k] == pytest.approx(v / 2) for k, v in lights.items())
    _, p3 = whatif.apply_scenario(s, p, "heat_pump")
    assert p3["shiftable_tasks"]["water_heater"]["efficiency"] == 3.0
    s4, _ = whatif.apply_scenario(s, p, "setpoint")
    assert s4["baseline_setpoint_c"] == s["baseline_setpoint_c"] + 1
    assert s == default_settings()                                  # 原設定不變
    with pytest.raises(KeyError):
        whatif.apply_scenario(s, p, "solar")


def test_units():
    s = default_settings()
    assert whatif.units(s, "ac_upgrade") == 4                      # 三間臥室 + 客廳
    assert whatif.units(s, "heat_pump") == 1
    s["appliance_edits"] = {"utility/water_heater": {"enabled": False}}
    assert whatif.units(s, "heat_pump") == 0                       # 瓦斯熱水器：不適用


def test_annual_estimate(results):
    cur = results["current"]
    assert list(cur["month"]) == list(range(1, 13))
    assert list(cur.loc[cur["proxy"], "month"]) == [10, 11, 12]     # 沒有天氣資料的月份
    assert 3000 < cur["kwh"].sum() < 9000                           # 4 人家庭一年
    assert cur.loc[cur["month"] == 7, "kwh"].item() > cur.loc[cur["month"] == 3, "kwh"].item()


def test_every_scenario_saves(results):
    cur = results["current"]
    for x in whatif.SCENARIOS:
        assert results[x.id]["kwh"].sum() < cur["kwh"].sum(), x.id
        assert results[x.id]["bill"].sum() < cur["bill"].sum(), x.id
    # 冷氣升級只影響夏季：冬季（1 月）幾乎不變
    jan = lambda df: df.loc[df["month"] == 1, "kwh"].item()   # noqa: E731
    assert jan(results["ac_upgrade"]) == pytest.approx(jan(cur), rel=0.03)


def test_payback_table_and_verdict():
    cur = pd.DataFrame({"kwh": [100.0] * 12, "bill": [1000.0] * 12})
    res = {"led": pd.DataFrame({"kwh": [90.0] * 12, "bill": [900.0] * 12}),       # 每年省 1,200 元
           "setpoint": pd.DataFrame({"kwh": [95.0] * 12, "bill": [950.0] * 12}),  # 每年省 600 元
           "heat_pump": pd.DataFrame({"kwh": [100.0] * 12, "bill": [1000.0] * 12})}
    t = whatif.payback_table(cur, res, {"led": 3600, "setpoint": 0, "heat_pump": 60000}).set_index("id")
    assert t.loc["led", "每年省(元)"] == pytest.approx(1200)
    assert t.loc["led", "回收年限(年)"] == pytest.approx(3.0)
    assert t.loc["led", "使用年限內淨效益(元)"] == pytest.approx(1200 * 10 - 3600)
    assert t.loc["setpoint", "回收年限(年)"] == 0.0
    assert pd.isna(t.loc["heat_pump", "回收年限(年)"])
    assert whatif.verdict(t.loc["led"]).endswith("划算")
    assert whatif.verdict(t.loc["setpoint"]) == "不用花錢，立刻可以做"
    assert whatif.verdict(t.loc["heat_pump"]) == "省不到電費，不建議"


def test_whatif_tab():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300)
    at.session_state["_test_page"] = "analysis"
    at.run()
    at.multiselect(key="whatif_choice").set_value(["led", "setpoint"]).run()
    next(b for b in at.button if b.label == "執行情境試算").click().run()
    assert not at.exception and not at.error
    labels = [m.label for m in at.metric]
    assert "目前年電費" in labels and "最划算" in labels
