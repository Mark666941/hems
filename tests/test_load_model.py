"""階段 4：負載與耗電模型。"""
import pytest

from core.data_source import CwaCsvWeatherSource, load_representative_days
from core.load_model import (
    LoadModel,
    SpaceGeometry,
    ac_cop,
    ac_power,
    cooling_load,
    effective_wall_temps,
    humidity_ratio,
    load_params,
    task_hourly,
)
from core.models import load_house
from core.schedule import Schedule


@pytest.fixture(scope="module")
def params():
    return load_params()


@pytest.fixture(scope="module")
def setup():
    house = load_house()
    model = LoadModel(house, Schedule(house))
    days = load_representative_days()
    src = CwaCsvWeatherSource()
    return house, model, src.get_hourly_weather(days["summer"]), src.get_hourly_weather(days["winter"])


# ---------- 物理公式 ----------

def test_humidity_ratio_matches_psychrometric_chart():
    # 濕空氣線圖：25°C、50% RH 約 0.0099 kg/kg
    assert humidity_ratio(25, 50) == pytest.approx(0.0099, abs=0.0002)


def test_no_load_when_indoor_equals_outdoor(params):
    house = load_house()
    geo = SpaceGeometry.from_space(house.get_space("master_bedroom"), params)
    rh_in = params["infiltration"]["indoor_rh_pct"]
    load = cooling_load(geo, params, t_out=26, rh_out=rh_in, solar_w_m2=0, t_wall_eff=26, t_set=26)
    assert load.total == pytest.approx(0, abs=1e-6)


def test_lower_setpoint_needs_more_cooling(params):
    geo = SpaceGeometry.from_space(load_house().get_space("living_room"), params)
    args = dict(t_out=33, rh_out=60, solar_w_m2=500, t_wall_eff=35)
    assert cooling_load(geo, params, t_set=24, **args).total > cooling_load(geo, params, t_set=28, **args).total


def test_wall_lag_preserves_daily_mean():
    sol_air = [25 + h for h in range(24)]
    eff = effective_wall_temps(sol_air, lag_h=4, decrement=0.4)
    assert sum(eff) / 24 == pytest.approx(sum(sol_air) / 24)
    assert max(eff) - min(eff) == pytest.approx(0.4 * 23)   # 振幅衰減
    assert eff.index(max(eff)) == (23 + 4) % 24              # 峰值延後 4 小時


def test_cop_trends(params):
    ac = params["air_conditioner"]
    assert ac_cop(35, 27, 1.0, ac) == pytest.approx(ac["rated_cop"])   # 額定條件
    assert ac_cop(38, 26, 0.6, ac) < ac_cop(30, 26, 0.6, ac)            # 室外越熱效率越差
    assert ac_cop(32, 28, 0.6, ac) > ac_cop(32, 24, 0.6, ac)            # 設定溫度越高效率越好
    assert ac_cop(32, 26, 0.1, ac) < ac_cop(32, 26, 0.4, ac)            # 低負載啟停效率下降


def test_ac_power_and_capacity_limit(params):
    ac = params["air_conditioner"]
    r = ac_power(1000, rated_power_w=800, t_out=33, t_set=26, ac=ac)
    assert r.power_w == pytest.approx(r.delivered_w / r.cop)
    assert r.unmet_w == 0
    big = ac_power(10000, rated_power_w=800, t_out=33, t_set=26, ac=ac)
    assert big.delivered_w == pytest.approx(800 * ac["rated_cop"])
    assert big.unmet_w > 0
    assert ac_power(-50, 800, 25, 26, ac).power_w == 0


# ---------- 整合 ----------

def test_ac_off_when_room_empty(setup):
    house, model, summer, _ = setup
    df = model.space_hourly(house.get_space("master_bedroom"), summer, "weekday", 26)
    assert (df.loc[8:17, "air_conditioner"] == 0).all()   # 上班時段沒人
    assert df.loc[0, "air_conditioner"] > 0                # 半夜睡覺有開


def test_pulldown_when_ac_starts(setup):
    house, model, summer, _ = setup
    df = model.space_hourly(house.get_space("master_bedroom"), summer, "weekday", 26)
    assert df.loc[22, "load_pulldown"] > 0   # 22 時回房開冷氣，要先移除白天累積的熱
    assert df.loc[2, "load_pulldown"] == 0   # 持續運轉時不需要


def test_lower_setpoint_uses_more_energy(setup):
    house, model, summer, _ = setup
    space = house.get_space("living_room")
    e24 = model.space_hourly(space, summer, "weekday", 24)["air_conditioner"].sum()
    e28 = model.space_hourly(space, summer, "weekday", 28)["air_conditioner"].sum()
    assert e24 > e28 > 0


def test_winter_uses_natural_ventilation(setup):
    house, model, _, winter = setup
    for space in house.spaces:
        df = model.space_hourly(space, winter, "weekday", 26)
        if "air_conditioner" in df:
            assert df["air_conditioner"].sum() == 0


def test_bath_heater_only_in_winter(setup):
    house, model, summer, winter = setup
    space = house.get_space("bathroom_1")
    assert model.space_hourly(space, summer, "weekday")["bath_heater"].sum() == 0
    assert model.space_hourly(space, winter, "weekday")["bath_heater"].sum() > 0


def test_water_heater_energy(setup):
    _, model, summer, winter = setup
    s = {t["appliance"]: t for t in model.shiftable_tasks(summer, "weekday")}
    w = {t["appliance"]: t for t in model.shiftable_tasks(winter, "weekday")}
    # 4 人 × 40 L × 4.186 ×（42 − 日均溫）÷ 3600 ÷ 0.85
    expected = 160 * 4.186 * (42 - summer.temp_c.mean()) / 3600 / 0.85
    assert s["water_heater"]["energy_kwh"] == pytest.approx(expected)
    assert w["water_heater"]["energy_kwh"] > s["water_heater"]["energy_kwh"]   # 冬天冷水較冷


def test_task_hourly_conserves_energy_and_wraps():
    task = {"power_w": 4000, "duration_h": 1.5, "default_start": 23}
    hourly = task_hourly(task)
    assert sum(hourly) == pytest.approx(4000 * 1.5)
    assert hourly[23] == 4000 and hourly[0] == 2000   # 跨午夜繞回清晨


def test_daily_total_in_reasonable_range(setup):
    """台灣四口之家夏季日用電約 10–25 kWh。"""
    house, model, summer, _ = setup
    total = 0.0
    for space in house.spaces:
        df = model.space_hourly(space, summer, "weekday", 26)
        total += df[[a.id for a in space.appliances if a.id in df]].sum().sum() / 1000
    total += sum(t["energy_kwh"] for t in model.shiftable_tasks(summer, "weekday"))
    assert 10 < total < 25
