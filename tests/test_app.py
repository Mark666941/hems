"""階段 8：Streamlit 介面（用 Streamlit 官方的 AppTest 在背景執行，不需開瀏覽器）。"""
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ui.settings import (apply_routine_times, apply_spaces_table, build_analyzer, default_settings,
                         routine_times, spaces_table, to_key)


@pytest.fixture
def app():
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=180)
    at.run()
    return at


def test_app_loads_without_errors(app):
    assert not app.exception and not app.error
    assert len(app.tabs) == 8
    labels = [m.label for m in app.metric]
    assert labels[:6] == ["用電", "流動電費", "尖峰占比", "舒適時間", "平均 PPD", "碳排放"]


def test_sidebar_interactions(app):
    app.sidebar.segmented_control[0].set_value("冬季代表日").run()
    assert not app.exception
    app.sidebar.radio[0].set_value("白天／夜間不同模式").run()
    assert not app.exception
    app.sidebar.segmented_control[0].set_value("自選日期").run()
    app.sidebar.date_input[0].set_value(date(2026, 4, 15)).run()
    app.sidebar.selectbox[0].set_value("progressive").run()
    assert not app.exception


def test_invalid_settings_show_error_instead_of_crash(app):
    s = app.session_state.settings
    rows = [r for r in spaces_table(s) if r["id"] != "master_bedroom"]   # 家長還在使用主臥
    app.session_state.settings = apply_spaces_table(s, rows)
    app.run()
    assert not app.exception
    assert any("master_bedroom" in e.value for e in app.error)


def test_launcher_finds_free_port():
    import socket

    import run_app
    port = run_app.free_port()
    with socket.socket() as s:        # 拿到的連接埠確實可以使用
        s.bind(("127.0.0.1", port))


# ---------- 設定轉換 ----------

def test_settings_key_is_stable():
    assert to_key(default_settings()) == to_key(default_settings())


def test_default_settings_reproduce_core_results():
    analyzer = build_analyzer(default_settings())
    table, _ = analyzer.compare_day(date(2026, 7, 20))
    assert table.loc[0, "用電(kWh)"] == pytest.approx(15.22, abs=0.05)   # 與階段 5 基準一致


def test_add_space_from_template():
    s = default_settings()
    rows = spaces_table(s) + [{"id": "", "名稱": "書房", "範本": "bedroom", "坪數": 3.0}]
    new = apply_spaces_table(s, rows)
    added = new["house"]["spaces"][-1]
    assert added["id"] == "bedroom_3" and added["area_ping"] == 3.0
    house = build_analyzer(new).sim.house
    assert house.get_space("bedroom_3").get_appliance("air_conditioner") is not None
    # 原有空間的客製化（次臥 1 的電腦、浴室 2 沒有暖風機）保留
    assert house.get_space("bedroom_1").get_appliance("computer") is not None
    assert house.get_space("bathroom_2").get_appliance("bath_heater") is None


def test_routine_time_edit():
    s = apply_routine_times(default_settings(), "adult_weekday",
                            {"wake": "06:30", "bed": "22:30", "out_start": "09:00", "out_end": "17:00"})
    assert routine_times(s["schedule"]["routines"]["adult_weekday"]) == {
        "wake": "06:30", "bed": "22:30", "out_start": "09:00", "out_end": "17:00"}
    home = build_analyzer(s).sim.schedule.people_at_home("weekday")
    assert home[17] > 2   # 大人 17:00 回家


def test_appliance_edit_disables_water_heater():
    s = default_settings()
    s["appliance_edits"] = {"utility/water_heater": {"power_w": 4000, "enabled": False}}
    tasks = build_analyzer(s).sim.load_model.shiftable_tasks(
        build_analyzer(s).sim.weather.get_hourly_weather(date(2026, 7, 20)), "weekday")
    assert "water_heater" not in {t["appliance"] for t in tasks}
