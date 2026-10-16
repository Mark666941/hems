"""第 5 批：家電使用紀錄（每週習慣、每日紀錄、帳單度數）與本月／下月預測。"""
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core.storage import Store
from core.tariff import Tariff
from ui import usage
from ui.settings import build_analyzer, default_settings

APP = str(Path(__file__).resolve().parent.parent / "app.py")
TV = {"space_id": "living_room", "appliance_id": "tv"}


@pytest.fixture
def store(tmp_path):
    return Store(f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}")


@pytest.fixture(scope="module")
def analyzer():
    return build_analyzer(default_settings())


@pytest.fixture(scope="module")
def july(analyzer):
    dates = {date(2026, 7, d) for d in range(1, 32)}
    return usage.simulate_month(analyzer, 2026, 7, "tou_2", dates)


def profiles_for(prof):
    """所有月份都用同一份概況（測試用）。"""
    return lambda year, month: (prof, (year, month) if month == 7 else (2026, 7))


# ---------- 資料庫 ----------

def test_records_crud_and_isolation(store):
    uid = store.register("mark", "secret123")
    other = store.register("amy", "secret123")
    pid = store.create_profile(uid, "我家", default_settings(), {})
    pid2 = store.create_profile(uid, "爸媽家", default_settings(), {})
    rid = store.add_record(uid, pid, "habit", {**TV, "weekdays": [0, 1], "start": 19, "end": 22})
    store.add_record(uid, pid, "bill", {"start": "2026-07-01", "end": "2026-07-31", "kwh": 500})
    assert [d["appliance_id"] for _, d in store.list_records(uid, pid, "habit")] == ["tv"]
    assert len(store.list_records(uid, pid)) == 2
    assert store.list_records(uid, pid2) == []            # 每個設定檔各自一份
    with pytest.raises(KeyError):
        store.list_records(other, pid)                    # 別人的設定檔不能讀
    with pytest.raises(ValueError):
        store.add_record(uid, pid, "unknown", {})
    store.delete_record(uid, pid, rid)
    assert len(store.list_records(uid, pid)) == 1
    store.delete_profile(uid, pid)                        # 刪除設定檔時紀錄一起刪
    with store.engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM usage_records").scalar() == 0


# ---------- 紀錄計算 ----------

def test_habit_hours_and_description():
    assert usage.habit_hours({"start": 19, "end": 22}) == 3
    assert usage.habit_hours({"start": 22, "end": 2}) == 4          # 跨午夜
    assert usage.describe_habit({"weekdays": [0, 1, 2, 3, 4], "start": 19, "end": 22}).startswith("平日 19:00")
    assert usage.describe_habit({"weekdays": [1, 3], "start": 8, "end": 9}).startswith("週二、四")


def test_recordable_appliances_exclude_air_conditioner(analyzer):
    keys = usage.recordable_appliances(analyzer)
    assert "living_room/tv" in keys
    assert not any(k.endswith("/air_conditioner") for k in keys)


def test_record_adjustment_replaces_simulated_usage(analyzer, july):
    powers = {k: p for k, (_, p) in usage.recordable_appliances(analyzer).items()}
    tv_w = powers["living_room/tv"]
    sim_tv = july["appliance"]["weekday"]["living_room/tv"]
    habit = {**TV, "weekdays": [0, 1, 2, 3, 4], "start": 19, "end": 23}
    monday, saturday = date(2026, 7, 20), date(2026, 7, 25)
    adj = usage.record_adjustment(monday, "weekday", {"habit": [habit]}, powers, july["appliance"])
    assert adj == pytest.approx(tv_w * 4 / 1000 - sim_tv)
    # 有每週習慣的家電，習慣裡沒有的星期視為不使用
    adj_sat = usage.record_adjustment(saturday, "holiday", {"habit": [habit]}, powers, july["appliance"])
    assert adj_sat == pytest.approx(-july["appliance"]["holiday"]["living_room/tv"])
    # 每日紀錄優先於每週習慣
    daily = {**TV, "date": monday.isoformat(), "hours": 1}
    adj_daily = usage.record_adjustment(monday, "weekday", {"habit": [habit], "daily": [daily]}, powers,
                                        july["appliance"])
    assert adj_daily == pytest.approx(tv_w * 1 / 1000 - sim_tv)


def test_source_month_uses_nearest_month():
    tariff = Tariff()
    months = [(2026, m) for m in range(1, 10)]
    assert usage.source_month(2026, 7, months, tariff) == (2026, 7)
    assert usage.source_month(2026, 10, months, tariff) == (2026, 9)      # 10 月用 9 月天氣
    assert usage.source_month(2026, 12, months, tariff) == (2026, 1)      # 12 月用 1 月天氣（跨年）
    src = usage.source_month(2026, 11, months, tariff)                    # 9 月與 1 月一樣近 → 同為非夏月的 1 月
    assert src == (2026, 1) and not tariff.is_summer(date(*src, 1))
    assert usage.source_month(2026, 11, [], tariff) is None


def test_proxy_month_uses_target_month_prices(analyzer, july):
    """以 7 月天氣代替 10 月時，電費依 10 月（非夏月）電價計算，比 7 月便宜。"""
    tariff = analyzer.sim.tariff
    prof = {**july, "by_date": {}}
    jul = usage.estimate_day(date(2026, 7, 20), lambda y, m: (prof, (2026, 7)), tariff, {}, {})
    octo = usage.estimate_day(date(2026, 10, 19), lambda y, m: (prof, (2026, 7)), tariff, {}, {})   # 同為週一
    assert octo["kwh"] == pytest.approx(jul["kwh"]) and octo["proxy"]
    assert octo["cost"] < jul["cost"]


# ---------- 預測與帳單 ----------

def test_forecast_month_matches_simulation(analyzer, july):
    tariff = analyzer.sim.tariff
    r = usage.forecast_month(2026, 7, "tou_2", profiles_for(july), tariff, {}, {})
    sim_total = sum(v["kwh"] for v in july["by_date"].values())
    assert r["kwh"] == pytest.approx(sim_total)
    assert r["low"] < r["kwh"] < r["high"] and r["bill_low"] < r["bill"] < r["bill_high"]
    assert not r["proxy"] and len(r["daily"]) == 31
    # 下月（8 月）用 7 月概況代替時標示 proxy
    assert usage.forecast_month(2026, 8, "tou_2", profiles_for(july), tariff, {}, {})["proxy"]


def test_bill_comparison_and_correction(analyzer, july):
    tariff = analyzer.sim.tariff
    sim_total = sum(v["kwh"] for v in july["by_date"].values())
    bills = [{"start": "2026-07-01", "end": "2026-07-31", "kwh": sim_total * 1.2}]
    table, factor = usage.compare_bills(bills, profiles_for(july), tariff, {}, {})
    assert factor == pytest.approx(1.2)
    assert table.loc[0, "誤差"] == pytest.approx(0.2)
    # 套用修正後，預測乘上係數；誤差變小，範圍收斂到最低 ±5%
    r = usage.forecast_month(2026, 7, "tou_2", profiles_for(july), tariff, {}, {}, factor=factor,
                             spread=usage.uncertainty(table, factor, True, False))
    assert r["kwh"] == pytest.approx(sim_total * 1.2)
    assert r["high"] / r["kwh"] == pytest.approx(1.05)
    assert usage.uncertainty(table, factor, False, False) == pytest.approx(0.2)
    assert usage.uncertainty(table.iloc[0:0], None, False, True) == pytest.approx(0.15)


# ---------- 介面 ----------

def test_forecast_page_with_records():
    at = AppTest.from_file(APP, default_timeout=180)
    at.session_state["_test_page"] = "forecast"
    at.run()
    assert not at.exception and not at.error
    html = " ".join(m.value for m in at.markdown)
    assert "月預測用電" in html and "月預測電費" in html

    # 新增每週習慣與帳單
    next(b for b in at.button if b.label == "新增每週習慣").click().run()
    assert not at.exception
    at.number_input(key="bill_kwh").set_value(600.0)
    next(b for b in at.button if b.label == "新增帳單").click().run()
    assert not at.exception and not at.error
    store = Store()
    records = store.list_records(at.session_state.user_id, at.session_state.profile_id)
    kinds = [d for _, d in records]
    assert any("weekdays" in d for d in kinds) and any(d.get("kwh") == 600.0 for d in kinds)

    # 帳單比對出現，開啟修正
    assert any("帳單合計比模擬" in m.value for m in at.markdown)
    at.toggle(key="forecast_correction_toggle").set_value(True).run()
    assert at.session_state.ui["forecast_correction"] is True
    assert not at.exception and not at.error

    # 刪除紀錄
    for b in [b for b in at.button if b.key and b.key.startswith("del_record_")][:1]:
        b.click().run()
    assert not at.exception
    assert len(store.list_records(at.session_state.user_id, at.session_state.profile_id)) == len(records) - 1
