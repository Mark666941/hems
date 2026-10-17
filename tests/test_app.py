"""階段 8：Streamlit 介面（用 Streamlit 官方的 AppTest 在背景執行，不需開瀏覽器）。"""
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ui.settings import (
    apply_routine_times,
    apply_spaces_table,
    build_analyzer,
    default_settings,
    routine_times,
    spaces_table,
    to_key,
)


@pytest.fixture
def app():
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=180)
    at.run()
    return at


def test_app_loads_without_errors(app):
    """首頁：家中此刻的四張大數字卡。"""
    assert not app.exception and not app.error
    html = " ".join(m.value for m in app.markdown)
    for label in ("此刻用電", "今日用電", "今日電費", "在家"):
        assert label in html


@pytest.mark.parametrize("page", ["plan", "analysis", "forecast", "settings", "about"])
def test_every_page_renders(page):
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=180)
    at.session_state["_test_page"] = page
    at.run()
    assert not at.exception and not at.error


def test_sidebar_menu(app):
    """左側為功能選單：每項一個按鈕＋目前設定摘要，點選才開啟設定視窗。"""
    labels = [b.label for b in app.sidebar.button]
    for title in ("📅 日期與時間", "💰 電價方案", "🎛️ 運轉模式", "🌡️ 舒適偏好", "🏠 格局與房間"):
        assert any(label.startswith(title) for label in labels)
    hour = app.session_state.ui["hour"]
    button = "▶" if hour < 23 else "◀"
    next(b for b in app.sidebar.button if b.label == button).click().run()
    assert app.session_state.ui["hour"] == (hour + 1 if hour < 23 else hour - 1)
    next(b for b in app.sidebar.button if b.label == "現在").click().run()   # 回到現在
    assert app.session_state.ui["hour"] == __import__("datetime").datetime.now().hour
    assert not app.exception


def test_settings_change_updates_summary(app):
    # 設定視窗按「套用」後寫入 session_state.ui；這裡直接修改狀態，模擬各種組合
    for changes in ({"preset": "冬季代表日"}, {"schedule_type": "switch"},
                    {"preset": "自選日期", "custom_day": date(2026, 4, 15)}, {"plan_id": "progressive"}):
        app.session_state.ui.update(changes)
        app.run()
        assert not app.exception
    captions = [c.value for c in app.sidebar.caption]
    assert any("2026-04-15" in c for c in captions)
    assert any("累進" in c for c in captions)
    assert any("→ 23:00 舒眠模式" in c for c in captions)


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
    from core.simulator import Simulator, load_baseline_plan
    core_kwh = Simulator().run(date(2026, 7, 20), "tou_2", load_baseline_plan()).total_kwh
    assert table.loc[0, "用電(kWh)"] == pytest.approx(core_kwh, abs=0.001)   # 介面設定與核心模組結果一致
    assert 10 < core_kwh < 25                                                  # 夏季平日全屋用電合理範圍


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
