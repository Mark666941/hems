"""階段 5：基準推演引擎。"""
from datetime import date

import pytest

from core.simulator import CATEGORIES, ControlPlan, Simulator, load_baseline_plan

SUMMER = date(2026, 7, 20)    # 週一
HOLIDAY = date(2026, 7, 18)   # 週六
WINTER = date(2026, 1, 20)    # 週二


@pytest.fixture(scope="module")
def sim():
    return Simulator()


@pytest.fixture(scope="module")
def summer(sim):
    return sim.run(SUMMER)


# ---------- 能量守恆與彙整一致 ----------

def test_energy_is_conserved_across_views(summer):
    total = summer.total_kwh
    assert summer.hourly["kwh"].sum() == pytest.approx(total)
    assert summer.by_space().sum() == pytest.approx(total)
    assert summer.by_category().sum() == pytest.approx(total)
    assert summer.hourly_by_category().to_numpy().sum() == pytest.approx(total)
    assert summer.hourly_by_space().to_numpy().sum() == pytest.approx(total)


def test_matches_load_model_per_space(sim, summer):
    # 推演引擎的冷氣用電要和負載模型直接算的一致（基準設定 25°C）
    weather = sim.weather.get_hourly_weather(SUMMER)
    df = sim.load_model.space_hourly(sim.house.get_space("living_room"), weather, "weekday", 25)
    engine = summer.records.query("space_id == 'living_room' and appliance_id == 'air_conditioner'")["kwh"].sum()
    assert engine == pytest.approx(df["air_conditioner"].sum() / 1000)


def test_cost_equals_kwh_times_price(summer):
    assert summer.energy_cost == pytest.approx((summer.hourly["kwh"] * summer.hourly["price"]).sum())


def test_essential_loads_run_all_day(summer):
    essential = summer.records.query("category == '必要負載'").groupby("hour")["kwh"].sum()
    assert len(essential) == 24
    assert (essential > 0).all()


def test_categories_complete(summer):
    assert list(summer.by_category().index) == CATEGORIES
    assert set(summer.records["category"]) <= set(CATEGORIES)


# ---------- 情境合理性 ----------

def test_summer_baseline_in_reasonable_range(summer):
    assert 12 < summer.total_kwh < 25
    assert summer.by_category()["冷氣"] > summer.total_kwh * 0.3   # 夏季冷氣是最大宗


def test_weekday_peak_hours_dominate_baseline(summer):
    periods = summer.kwh_by_period()
    assert periods["尖峰"] > periods["離峰"]   # 習慣用電集中在晚上尖峰
    assert 16 <= summer.peak_hour <= 22


def test_holiday_all_off_peak_and_more_usage(sim, summer):
    holiday = sim.run(HOLIDAY)
    assert holiday.day_type == "holiday"
    assert set(holiday.hourly["period_label"]) == {"離峰"}
    assert holiday.total_kwh > summer.total_kwh   # 假日在家時間長


def test_winter_no_ac(sim):
    winter = sim.run(WINTER)
    assert winter.by_category()["冷氣"] == 0
    assert winter.by_category()["熱水與洗烘"] > 5   # 冬天熱水用電大


def test_room_temperature_is_physical(summer):
    c = summer.climate
    cooled = c[(c["ac_on"] >= 1) & (c["load_total_w"] > 0)]
    diff = cooled["room_temp_c"] - cooled["setpoint_c"].astype(float)
    # 全時運轉時大多維持設定溫度；剛開機那小時蓄熱可能還沒移除完，室溫略高但不會低於設定值
    assert (diff.abs() < 0.01).mean() > 0.9
    assert diff.min() > -0.01 and diff.max() < 1.0
    # 白天沒人、冷氣關掉後，室溫因熱容量逐漸上升（不會瞬間跳到室外溫度）
    empty = c[(c["space_id"] == "master_bedroom") & (c["hour"].between(8, 15))]["room_temp_c"].tolist()
    assert empty[0] < empty[-1]
    assert empty[-1] > 30
    assert c["room_temp_c"].max() < 40         # 不會發散
    assert summer.summary()["冷氣能力不足(kWh)"] < 1


def test_winter_room_temperature_without_heating(sim):
    # 台中冬季無暖氣公寓，室內約 18–23°C（比室外 14–22°C 略暖）
    c = sim.run(WINTER).climate
    bedrooms = c[c["space_id"].isin(["master_bedroom", "bedroom_1", "bedroom_2", "living_room"])]
    assert bedrooms["room_temp_c"].between(17, 25).all()


# ---------- ControlPlan（階段 6 的介面）----------

def test_shifting_tasks_to_off_peak_saves_money(sim, summer):
    shifted = sim.run(SUMMER, control=ControlPlan(default_setpoint=25,
                                                  task_starts={"water_heater": 1, "washer": 3}))
    assert shifted.total_kwh == pytest.approx(summer.total_kwh)       # 用電量不變
    assert shifted.energy_cost < summer.energy_cost                    # 電費下降
    assert shifted.kwh_by_period()["尖峰"] < summer.kwh_by_period()["尖峰"]


def test_higher_setpoint_uses_less_ac(sim, summer):
    warm = sim.run(SUMMER, control=ControlPlan(default_setpoint=28))
    assert warm.by_category()["冷氣"] < summer.by_category()["冷氣"]


def test_ac_on_override(sim):
    off = {sid: [0.0] * 24 for sid in ("master_bedroom", "bedroom_1", "bedroom_2", "living_room")}
    result = sim.run(SUMMER, control=ControlPlan(ac_on=off))
    assert result.by_category()["冷氣"] == 0


def test_baseline_plan_from_config():
    plan = load_baseline_plan()
    assert plan.default_setpoint == 25
    assert plan.setpoint_for("anything") == [25] * 24


# ---------- 電價方案與整月 ----------

def test_reprice_keeps_energy(sim, summer):
    tou3 = sim.reprice(summer, "tou_3")
    assert tou3.total_kwh == pytest.approx(summer.total_kwh)
    assert tou3.energy_cost != pytest.approx(summer.energy_cost)
    assert tou3.hourly.loc[17, "price"] == 7.13


def test_tou_monthly_bill_matches_taipower_example(sim):
    # 台電範例：二段式 8 月尖峰 1,200 度、離峰 1,000 度，總電費 8,535 元
    energy = 5.16 * 1200 + 2.06 * 1000
    bill = sim.tariff.tou_monthly_bill("tou_2", energy, 2200)
    assert round(bill["total"]) == 8535


def test_month_run(sim):
    month = sim.run_month(2026, 7)
    daily = month.daily()
    assert len(daily) == 31
    assert month.total_kwh == pytest.approx(daily["kwh"].sum())
    assert 400 < month.total_kwh < 800   # 台灣四口之家夏季月用電合理範圍
    bills = month.bills()
    assert set(bills["plan_id"]) == {"tou_2", "tou_3", "progressive"}
    assert (bills["月電費"] > 0).all()
