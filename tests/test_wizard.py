"""C1：新手引導精靈（房型、成員作息、電價、熱水器）。"""
from datetime import date
from pathlib import Path

from streamlit.testing.v1 import AppTest

from ui.settings import build_analyzer, default_settings
from ui.wizard import apply_wizard, default_draft

APP = str(Path(__file__).resolve().parent.parent / "app.py")
DAY = date(2026, 7, 20)


def _baseline_kwh(settings: dict) -> float:
    a = build_analyzer(settings)
    return a.sim.run(DAY, "tou_2", a.opt.baseline_plan).total_kwh


def test_default_draft_reads_current_settings():
    d = default_draft(default_settings(), {"plan_id": "tou_2"})
    assert (d["bedrooms"], d["bathrooms"], d["adults"], d["children"]) == (3, 2, 2, 2)
    assert d["bedroom_ping"] == [5.0, 4.0, 3.5] and d["living_ping"] == 8.0
    assert d["water_heater"] == "electric" and d["plan_id"] == "tou_2"
    assert d["adult_times"]["out_start"] == "08:00"


def test_unchanged_answers_keep_results():
    s = default_settings()
    new, ui = apply_wizard(s, {"plan_id": "tou_2"}, default_draft(s, {"plan_id": "tou_2"}))
    assert ui["onboarded"] is True
    assert [x["id"] for x in new["house"]["spaces"]] == [x["id"] for x in s["house"]["spaces"]]
    assert abs(_baseline_kwh(new) - _baseline_kwh(s)) < 1e-6


def test_smaller_home_with_gas_heater():
    s = default_settings()
    draft = {**default_draft(s, {}), "bedrooms": 2, "bathrooms": 1, "bedroom_ping": [6.0, 4.0],
             "adults": 1, "children": 1, "water_heater": "gas", "plan_id": "progressive",
             "adult_times": {"wake": "06:30", "bed": "22:30", "out_start": "09:00", "out_end": "17:00"}}
    new, ui = apply_wizard(s, {"plan_id": "tou_2"}, draft)
    spaces = new["house"]["spaces"]
    assert sum(x["template"] == "bedroom" for x in spaces) == 2
    assert sum(x["template"] == "bathroom" for x in spaces) == 1
    assert next(x for x in spaces if x["id"] == "master_bedroom")["area_ping"] == 6.0
    members = new["schedule"]["members"]
    assert [m["role"] for m in members] == ["adult", "child"]
    ids = {x["id"] for x in spaces}
    assert all(m["bedroom"] in ids and m["bathroom"] in ids for m in members)
    assert ui["plan_id"] == "progressive"
    a = build_analyzer(new)                                    # 設定有效、可以推演
    tasks = a.sim.load_model.shiftable_tasks(a.sim.weather.get_hourly_weather(DAY), "weekday")
    assert "water_heater" not in {t["appliance"] for t in tasks}  # 瓦斯熱水器不計入用電
    home = a.sim.schedule.people_at_home("weekday")
    assert home[17] >= 1                                       # 大人 17:00 回家
    assert _baseline_kwh(new) < _baseline_kwh(s)               # 房子小、人少、沒有電熱水器


def test_bigger_home_spreads_children():
    s = default_settings()
    draft = {**default_draft(s, {}), "bedrooms": 4, "children": 3, "bedroom_ping": [5.0, 4.0, 3.5, 3.0]}
    new, _ = apply_wizard(s, {}, draft)
    bedrooms = [x["id"] for x in new["house"]["spaces"] if x["template"] == "bedroom"]
    assert len(bedrooms) == 4
    kids = [m["bedroom"] for m in new["schedule"]["members"] if m["role"] == "child"]
    assert kids == bedrooms[1:]                                # 三個小孩各一間
    assert build_analyzer(new).sim.house.get_space(bedrooms[-1]).get_appliance("air_conditioner") is not None


def test_wizard_dialog_flow():
    at = AppTest.from_file(APP, default_timeout=180)
    at.run()
    assert any(b.label.startswith("🧭 新手引導") for b in at.sidebar.button)
    at.session_state.ui["onboarded"] = False
    at.run()
    next(b for b in at.button if b.label == "開始設定").click().run()
    assert not at.exception
    for _ in range(3):
        next(b for b in at.button if b.label == "下一步 →").click().run()
        assert not at.exception
    at.radio(key="wz_heater").set_value("gas").run()
    next(b for b in at.button if b.label == "完成").click().run()
    assert not at.exception and not at.error
    assert at.session_state.ui["onboarded"] is True
    edits = at.session_state.settings["appliance_edits"]
    assert edits["utility/water_heater"]["enabled"] is False
    assert not any(b.label == "開始設定" for b in at.button)      # 完成後首頁不再顯示引導提示
