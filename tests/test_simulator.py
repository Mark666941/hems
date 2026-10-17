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
    weather = sim.load_model.with_solar_geometry(sim.weather.get_hourly_weather(SUMMER), SUMMER)   # 同引擎：加上方位日射
    c = summer.climate                                                       # 同引擎：鄰房室溫（隔間熱交換）
    neighbors = {sid: list(g.sort_values("hour")["room_temp_c"]) for sid, g in c.groupby("space_id")}
    df = sim.load_model.space_hourly(sim.house.get_space("living_room"), weather, "weekday", 25,
                                     neighbor_temps=neighbors)
    engine = summer.records.query("space_id == 'living_room' and appliance_id == 'air_conditioner'")["kwh"].sum()
    assert engine == pytest.approx(df["air_conditioner"].sum() / 1000, rel=0.01)   # 引擎迭代到 0.05°C 內收斂


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
    # 剛開機那小時（前一小時沒有全時運轉）蓄熱可能還沒移除完，室溫略高（鄰房經隔間牆傳熱時可達約 1°C）；
    # 之後持續運轉的小時都維持設定溫度；任何時候都不會低於設定值
    prev_on = c.set_index(["space_id", "hour"])["ac_on"]
    startup = [prev_on.get((sid, (h - 1) % 24), 0) < 1 for sid, h in zip(cooled["space_id"], cooled["hour"])]
    assert (diff.abs() < 0.01).mean() > 0.9
    assert diff.min() > -0.01
    assert diff[[not x for x in startup]].abs().max() < 0.01
    assert diff[startup].max() < 1.5
    # 白天沒人、冷氣關掉後，室溫因熱容量逐漸上升（不會瞬間跳到室外溫度）
    empty = c[(c["space_id"] == "master_bedroom") & (c["hour"].between(8, 15))]["room_temp_c"].tolist()
    assert empty[0] < empty[-1]
    assert empty[-1] > 30
    assert c["room_temp_c"].max() < 40         # 不會發散
    assert summer.summary()["冷氣能力不足(kWh)"] < 1


def test_winter_room_temperature_without_heating(sim):
    # 台中冬季無暖氣公寓，室內約 18–23°C（比室外 14–22°C 略暖）；
    # 朝南的房間冬天有陽光直射進窗（被動式太陽能得熱），白天可再暖 2–3°C
    c = sim.run(WINTER).climate
    north = c[c["space_id"].isin(["bedroom_2"])]["room_temp_c"]
    south = c[c["space_id"].isin(["master_bedroom", "living_room"])]["room_temp_c"]
    assert north.between(17, 23).all()
    assert south.between(17, 26).all()
    assert south.max() > north.max() + 2


# ---------- ControlPlan（階段 6 的介面）----------

def test_shifting_tasks_to_off_peak_saves_money(sim, summer):
    shifted = sim.run(SUMMER, control=ControlPlan(default_setpoint=25,
                                                  task_starts={"water_heater": 1, "washer": 3}))
    # 提早燒熱水要多付儲水桶散熱的耗電，但離峰電價便宜得多，電費仍下降
    assert shifted.total_kwh > summer.total_kwh
    assert shifted.energy_cost < summer.energy_cost
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


def test_interzone_heat_is_conserved(summer):
    # A2：隔間熱流「A 房流出 = B 房流入」，全屋加總約為 0（迭代收斂誤差內）
    c = summer.climate
    total = c.groupby("hour")["interzone_w"].sum().abs()
    scale = c["interzone_w"].abs().groupby(c["hour"]).sum()
    assert (total <= 0.05 * scale + 15).all()
    # 開冷氣的客廳被較熱的鄰房包圍，晚上會從隔間牆得熱
    living = c[(c["space_id"] == "living_room") & (c["ac_on"] >= 1)]
    assert living["interzone_w"].mean() > 100
