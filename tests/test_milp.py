"""B3：全屋混合整數規劃（MILP）最佳化。"""
from datetime import date

import pytest

from core.load_model import cooling_load
from core.milp import optimize_milp
from core.optimizer import ModeSchedule, Optimizer

DAY = date(2026, 7, 20)


@pytest.fixture(scope="module")
def opt():
    return Optimizer()


def test_linear_model_matches_cooling_load(opt):
    lm = opt.sim.load_model
    space = lm.house.get_space("bedroom_1")
    weather = lm.with_solar_geometry(opt.sim.weather.get_hourly_weather(DAY), DAY)
    lin = lm.linear_model(space, weather, "weekday", [1.0] * 24)
    prep = lm._prepare(space, weather, "weekday", [1.0] * 24, True, None, None)
    h = 15
    exact = cooling_load(prep["geo"], lm.params, prep["temps"][h], prep["rhs"][h], prep["solar"][h], prep["t_wall"][h],
                         26.0, prep["ppl_s"][h], prep["ppl_l"][h], prep["base_internal"][h], 1.0, 0.0,
                         prep["vertical"][h]).sensible
    assert lin["a"][h] - lin["b"][h] * 26.0 == pytest.approx(exact)      # 顯熱負載對室溫是線性的
    assert lin["b"][h] > 0 and all(c > 1 for c in lin["cop"])


def test_milp_plan_is_valid(opt):
    r = optimize_milp(opt, DAY, "normal")
    info = r.levels
    assert info["method"] == "milp" and info["binaries"] > 0 and info["solve_s"] < 10
    lo, hi = opt.modes["normal"].params.setpoint_range_c
    for sid, sps in r.plan.setpoints.items():
        on = r.plan.ac_on[sid]
        assert all(lo <= sp <= hi for sp, o in zip(sps, on) if o > 0)          # 設定溫度在模式範圍內
        assert all(abs(sp * 2 - round(sp * 2)) < 1e-9 for sp in sps)          # 0.5°C 間隔
    tasks = {t["appliance"]: t for t in r.result.tasks}
    assert all(t["feasible"] for t in tasks.values())                          # 熱水在洗澡前燒好
    if "washer" in tasks and "dryer" in tasks:
        wait = (tasks["dryer"]["start"] - (tasks["washer"]["start"] + tasks["washer"]["duration_h"])) % 24
        assert wait <= opt.sim.load_model.params["shiftable_tasks"]["dryer"]["max_wait_after_washer_h"] + 1e-9


def test_hourly_relaxes_grouped(opt):
    # 同一個線性模型（不做序列更新）：逐時版本的可行解包含分組版本，最佳目標值不會比較差
    hourly = optimize_milp(opt, DAY, "normal", refine=0).levels["objective"]
    grouped = optimize_milp(opt, DAY, "normal", hourly_setpoints=False, refine=0).levels["objective"]
    assert hourly <= grouped + 1e-6


def test_optimize_method_switch(opt):
    a = opt.optimize(DAY, "saving")
    b = opt.optimize(DAY, "saving", method="milp")
    assert b.levels["method"] == "milp" and "method" not in a.levels
    schedule = ModeSchedule.single(opt.modes["saving"])
    for r in (a, b):
        e = opt.evaluate(r.result, schedule)
        assert e["目標值(元)"] == pytest.approx(e["電費(元)"] + e["舒適懲罰(元)"] + opt.demand_weight * e["尖峰需量(kW)"])


def test_hybrid_never_worse_than_its_milp_start(opt):
    # 局部搜尋只接受讓目標值變好的調整，所以混合法不會比它的 MILP 起點差（同一個完整推演評估）
    schedule = ModeSchedule.single(opt.modes["normal"])
    milp = opt.evaluate(optimize_milp(opt, DAY, "normal").result, schedule)["目標值(元)"]
    hybrid_r = opt.optimize(DAY, "normal", method="hybrid")
    hybrid = opt.evaluate(hybrid_r.result, schedule)["目標值(元)"]
    assert hybrid_r.levels["method"] == "hybrid" and hybrid_r.levels["local_evaluations"] > 0
    assert hybrid <= milp + 0.5      # 局部搜尋以房間分別評估（鄰房室溫固定），全屋推演允許些微差異
    lo, hi = opt.modes["normal"].params.setpoint_range_c
    for sid, sps in hybrid_r.plan.setpoints.items():
        assert all(lo <= v <= hi for v, o in zip(sps, hybrid_r.plan.ac_on[sid]) if o > 0)
