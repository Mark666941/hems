"""階段 6：四種模式與最佳化。"""
from datetime import date

import pytest

from core.optimizer import (
    MODE_ORDER,
    ModeSchedule,
    Optimizer,
    _in_window,
    discomfort,
    load_modes,
    sleep_curve,
)

SUMMER = date(2026, 7, 20)   # 週一
WINTER = date(2026, 1, 20)   # 週二
BEDROOMS = ["master_bedroom", "bedroom_1", "bedroom_2"]


@pytest.fixture(scope="module")
def opt():
    return Optimizer()


@pytest.fixture(scope="module")
def summer(opt):
    return opt.compare(SUMMER)


@pytest.fixture(scope="module")
def winter(opt):
    return opt.compare(WINTER)


def row(table, name):
    return table[table["模式"] == name].iloc[0]


# ---------- 設定與工具函式 ----------

def test_modes_loaded_from_config():
    comfort, modes, demand_weight = load_modes()
    assert list(modes) == MODE_ORDER
    assert modes["sleep"].night is not None
    assert modes["sleep"].params_at(2).follow_sleep_curve       # 夜間跟隨睡眠曲線
    assert not modes["sleep"].params_at(15).follow_sleep_curve  # 白天同正常模式
    assert modes["saving"].params.setpoint_range_c == (26, 29)
    assert demand_weight > 0
    assert set(comfort["seasons"]) == {"cooling", "heating"}


def test_window_wraps_midnight():
    assert _in_window(23, 23, 7) and _in_window(3, 23, 7)
    assert not _in_window(7, 23, 7) and not _in_window(12, 23, 7)
    assert _in_window(10, 9, 17) and not _in_window(17, 9, 17)


def test_sleep_curve_rises_then_returns_before_wake():
    is_sleep = [h < 7 or h == 23 for h in range(24)]       # 23:00–07:00
    curve = sleep_curve(is_sleep, rise_per_h=0.5, max_rise=2.0, return_before_wake_h=1)
    assert [curve[h] for h in [23, 0, 1, 2, 3, 4, 5, 6]] == [0, 0.5, 1.0, 1.5, 2.0, 2.0, 2.0, 0.0]
    assert curve[12] == 0


def test_occupied_blocks_wrap_midnight():
    presence = [1 if h < 7 or h >= 22 else 0 for h in range(24)]
    presence[17] = 0.5
    blocks = Optimizer.occupied_blocks(presence)
    assert [22, 23, 0, 1, 2, 3, 4, 5, 6] in blocks
    assert [17] in blocks


def test_discomfort_deadband():
    assert discomfort(26.4, 26, 0.5) == 0
    assert discomfort(27.5, 26, 0.5) == pytest.approx(1.0)
    assert discomfort(24.0, 26, 0.5) == pytest.approx(1.5)


def test_mode_schedule_switch(opt):
    s = ModeSchedule.switch(opt.modes, "normal", "sleep", 23, 7)
    assert s.by_hour[22] == "normal" and s.by_hour[23] == "sleep" and s.by_hour[6] == "sleep"
    assert "23:00" in s.name


# ---------- 夏季：模式之間的取捨 ----------

def test_all_modes_cheaper_than_baseline(summer):
    table, _ = summer
    base = row(table, "基準（一般習慣）")["流動電費(元)"]
    for mid in MODE_ORDER:
        assert row(table, _name(mid))["流動電費(元)"] < base


def test_cost_and_comfort_tradeoff(summer):
    table, _ = summer
    saving, normal, perf = (row(table, n) for n in ("省電模式", "正常模式", "效能模式"))
    # 越省電的模式電費越低，但離偏好溫度越遠
    assert saving["流動電費(元)"] < normal["流動電費(元)"] < perf["流動電費(元)"]
    assert perf["平均溫差(°C)"] <= normal["平均溫差(°C)"] <= saving["平均溫差(°C)"]
    assert perf["舒適時間比例(%)"] == pytest.approx(100)


def test_baseline_overcooling_is_uncomfortable(summer):
    # 基準 25°C 整夜，入睡後過冷：舒適度反而不如正常模式
    table, _ = summer
    assert row(table, "基準（一般習慣）")["舒適時間比例(%)"] < row(table, "正常模式")["舒適時間比例(%)"]


def test_setpoints_within_mode_range(opt, summer):
    _, results = summer
    for mid in MODE_ORDER:
        r = results[mid]
        for h in range(24):
            lo, hi = opt.modes[mid].params_at(h).setpoint_range_c
            for sid in r.plan.setpoints:
                if r.plan.ac_on[sid][h] > 0:
                    assert lo <= r.plan.setpoints[sid][h] <= hi


def test_normal_mode_uses_one_setpoint_per_room(summer):
    _, results = summer
    r = results["normal"]
    for sid in BEDROOMS:
        used = {sp for sp, on in zip(r.plan.setpoints[sid], r.plan.ac_on[sid]) if on > 0}
        assert len(used) == 1   # 一般習慣：整天同一個設定溫度


def test_sleep_mode_follows_sleep_curve(summer):
    table, results = summer
    sp = results["sleep"].plan.setpoints["master_bedroom"]
    assert sp[0] < sp[2] < sp[4]          # 入睡後逐步調高
    assert sp[6] < sp[4]                  # 起床前回溫
    assert row(table, "舒眠模式")["平均溫差(°C)"] < row(table, "正常模式")["平均溫差(°C)"]


def test_performance_precools_before_arrival(summer):
    _, results = summer
    perf, normal = results["performance"].plan.ac_on, results["normal"].plan.ac_on
    # 小孩 16:30 到家：效能模式 15 時就開始預冷
    assert perf["bedroom_1"][15] > 0 and normal["bedroom_1"][15] == 0


def test_essential_loads_never_change(summer):
    _, results = summer
    def essential(r):
        return r.records.query("category == '必要負載'")["kwh"].sum()
    base = essential(results["基準"])
    for mid in MODE_ORDER:
        assert essential(results[mid].result) == pytest.approx(base)


def test_saving_mode_dims_lighting(summer):
    _, results = summer
    def lighting(r):
        return r.records.query("appliance_id == 'lighting'")["kwh"].sum()
    assert lighting(results["saving"].result) == pytest.approx(lighting(results["基準"]) * 0.7)


# ---------- 可移轉負載 ----------

def test_tasks_shifted_to_off_peak_and_feasible(summer):
    _, results = summer
    for mid in ("saving", "normal", "sleep"):
        r = results[mid].result
        heater = next(t for t in r.tasks if t["appliance"] == "water_heater")
        assert heater["feasible"]                          # 洗澡前燒好
        prices = r.hourly["price"]
        for t in r.tasks:
            assert prices[int(t["start"])] == prices.min()  # 從離峰開始


def test_quiet_hours_respected(opt, summer):
    _, results = summer
    sleeping = opt.sim.schedule.occupancy("weekday", activity="sleep").sum(axis=1)
    records = results["normal"].result.records
    noisy = records[records["appliance_id"].isin(["washer", "dryer"]) & (records["kwh"] > 0)]
    assert all(sleeping[h] < 1 for h in noisy["hour"])   # 正常模式不在睡眠時段洗衣


def test_performance_keeps_habits(summer):
    _, results = summer
    for t in results["performance"].result.tasks:
        assert t["start"] == t["default_start"]


def test_winter_shifting_saves_and_lowers_demand(winter):
    table, results = winter
    base, normal = row(table, "基準（一般習慣）"), row(table, "正常模式")
    assert normal["流動電費(元)"] < base["流動電費(元)"] * 0.8
    assert normal["最大需量(kW)"] <= base["最大需量(kW)"]   # 需量懲罰讓大功率家電錯開
    assert results["normal"].result.by_category()["冷氣"] == 0


def test_progressive_plan_does_not_shift_loads(opt):
    r = opt.optimize(SUMMER, "normal", "progressive")
    assert set(r.result.hourly["period_label"]) == {"不分時段"}
    washer = next(t for t in r.result.tasks if t["appliance"] == "washer")
    assert washer["start"] == washer["default_start"]   # 各時段同價，沒有移轉的理由


def _name(mid: str) -> str:
    return {"saving": "省電模式", "normal": "正常模式", "performance": "效能模式", "sleep": "舒眠模式"}[mid]
