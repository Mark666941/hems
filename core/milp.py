"""最佳化升級（優化 B3）：整間房子一起求解的混合整數線性規劃（MILP），求解器為 scipy 內建的 HiGHS。

和原本方法（core/optimizer.py：逐房座標下降 + 可移轉負載窮舉）的差別：
1. 全屋聯合：所有房間的室溫在同一個模型裡，隔間熱交換是方程式的一部分（原本逐房最佳化時把鄰房室溫當定值）
2. 逐時設定：每個房間每小時各自一個溫度（原本每個時段一個值）；可用 hourly_setpoints=False 改回分組，
   用來區分「全屋聯合」與「逐時自由」各自帶來多少改善
3. 冷氣與可移轉負載一起考慮全屋尖峰需量（原本只讓熱水器、洗衣、烘衣彼此錯開）

模型（每個房間 r、每小時 h，前一小時 h−1 循環到 23 時，與推演引擎一樣假設每天週期性重複）：
    熱容 C ×（T[r,h] − T[r,h−1]）÷ 3600 = 外界得熱 + Σ 隔間 UA ×（T[鄰,h] − T[r,h]）− 冷氣供冷 Q[r,h]
    冷氣運轉時外界得熱 = a − b × T（load_model.linear_model）；沒開時 = UA_外 ×（等效外界溫度 − T）+ 室內得熱
    冷氣耗電 = Q ÷ COP；舒適懲罰的平方項以分段線性逼近（凸函數，不需要整數變數）
    可移轉負載：每個可行開始時間一個 0/1 變數，恰選一個；烘衣只能在洗衣結束後 max_wait 小時內開始
目標：電費 + 舒適權重 × 人數 × 超出容許偏差的度數² + 需量權重 × 全屋最大用電（kW）
求解後轉成冷氣設定溫度與家電開始時間（ControlPlan），再交給完整的非線性推演驗算。
"""
from __future__ import annotations

import copy
import os
import sys
import time
from contextlib import contextmanager
from datetime import date

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from core.load_model import place_task, task_hourly
from core.schedule import day_type_for
from core.simulator import ControlPlan

# 平方懲罰 x² 的分段線性逼近：各段寬度與斜率（斷點 0, 0.5, 1, 2, 3, ∞；斜率 = 兩端點和）
SEGMENTS = [(0.5, 0.5), (0.5, 1.5), (1.0, 3.0), (1.0, 5.0), (np.inf, 7.0)]
SETPOINT_STEP_C = 0.5
UNMET_PENALTY = 100.0     # 元 / °C·h：室溫高於設定（冷氣能力不足）的懲罰，只為避免無解


@contextmanager
def _quiet_stdout():
    """暫時把標準輸出（檔案描述元 1）導向空裝置。"""
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):   # 沒有真正的檔案描述元（例如測試擷取輸出）
        yield
        return
    sys.stdout.flush()
    saved, devnull = os.dup(fd), os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, fd)
    try:
        yield
    finally:
        os.dup2(saved, fd)
        os.close(saved)
        os.close(devnull)


class _Model:
    """稀疏矩陣形式的線性模型（scipy.optimize.milp 的輸入）。"""

    def __init__(self):
        self.lb, self.ub, self.c, self.integer = [], [], [], []
        self.rows, self.cols, self.vals, self.row_lo, self.row_hi = [], [], [], [], []

    def var(self, lb=-np.inf, ub=np.inf, cost=0.0, integer=False) -> int:
        self.lb.append(lb)
        self.ub.append(ub)
        self.c.append(cost)
        self.integer.append(1 if integer else 0)
        return len(self.c) - 1

    def add(self, terms: dict[int, float], lo: float, hi: float) -> None:
        r = len(self.row_lo)
        for col, val in terms.items():
            if val:
                self.rows.append(r)
                self.cols.append(col)
                self.vals.append(val)
        self.row_lo.append(lo)
        self.row_hi.append(hi)

    def solve(self, time_limit: float):
        a = coo_matrix((self.vals, (self.rows, self.cols)), shape=(len(self.row_lo), len(self.c))).tocsr()
        with _quiet_stdout():   # HiGHS 的 C++ 程式偶爾直接印除錯訊息，disp=False 關不掉
            return milp(np.array(self.c), constraints=LinearConstraint(a, self.row_lo, self.row_hi),
                    integrality=np.array(self.integer), bounds=Bounds(self.lb, self.ub),
                    options={"time_limit": time_limit, "disp": False})


def optimize_milp(opt, day: date, mode, plan_id: str = "tou_2", hourly_setpoints: bool = True,
                  refine: int = 2, time_limit: float = 20.0):
    """以 MILP 求一天的控制策略。opt 為 core.optimizer.Optimizer；回傳 OptimizationResult（levels 內含求解資訊）。"""
    from core.optimizer import NOISY_TASKS, TASK_STEP_H, ModeSchedule, OptimizationResult

    t0 = time.perf_counter()
    sim, lm = opt.sim, opt.sim.load_model
    schedule = mode if isinstance(mode, ModeSchedule) else ModeSchedule.single(opt.modes[mode])
    weather = lm.with_solar_geometry(sim.weather.get_hourly_weather(day), day)
    day_type = day_type_for(day, sim.tariff.is_off_peak_day)
    prices = list(sim.tariff.hourly_prices(day, plan_id)["price"])
    temps = list(weather["temp_c"])
    season, prefs = opt.season(day)
    ctx_all = opt.contexts(day_type, season)

    plan = ControlPlan(name=f"{schedule.name}（MILP）", default_setpoint=prefs["preferred_awake_c"])
    for space in sim.house.spaces:
        factors = opt._appliance_factors(space, schedule)
        if factors:
            plan.appliance_factors[space.id] = factors
    ac_ids = {s.id for s in opt.ac_spaces}
    for space in opt.ac_spaces:
        plan.ac_on[space.id] = opt._ac_on(space, schedule, day_type, temps)

    rooms = [s for s in sim.house.spaces if s.area_ping > 0]
    lin = {s.id: lm.linear_model(s, weather, day_type, plan.ac_on.get(s.id, [0.0] * 24),
                                 plan.appliance_factors.get(s.id)) for s in rooms}
    def solve_once(cop_override: dict) -> dict:
        """建立並求解一次 MILP，把解寫進 plan（設定溫度、家電開始時間），回傳求解資訊。"""
        m = _Model()
        T = {(s.id, h): m.var() for s in rooms for h in range(24)}
        cooling = {}          # (房間, 時) → Q 變數（只有冷氣運轉的小時）
        ac_elec_terms = {h: {} for h in range(24)}   # 每小時冷氣耗電（W）= Σ Q / COP
        const_w = [0.0] * 24                          # 每小時固定用電（其他家電、潛熱耗電）
        for s in rooms:
            const_w = [c + w for c, w in zip(const_w, lin[s.id]["other_w"])]
        group_vars = {}

        for s in rooms:
            L = lin[s.id]
            cap = L["heat_capacity_j_k"] / 3600                 # W/K（每小時）
            on = plan.ac_on.get(s.id, [0.0] * 24)
            neighbors = lm.interzone_ua.get(s.id, {})
            ctx = ctx_all[s.id]
            for h in range(24):
                running = s.id in ac_ids and on[h] >= 0.5
                ua_iz = sum(neighbors.values())
                terms = {T[(s.id, h)]: cap + ua_iz, T[(s.id, (h - 1) % 24)]: -cap}
                for nid, ua in neighbors.items():
                    terms[T[(nid, h)]] = terms.get(T[(nid, h)], 0.0) - ua
                if running:
                    p = opt._mode_params(schedule, h)
                    lo, hi = p.setpoint_range_c
                    # 下限是硬限制（冷氣不會冷到低於設定範圍）；上限是軟限制：冷氣能力不足時室溫只能高於範圍，
                    # 每超出 1°C·h 罰 UNMET_PENALTY 元（避免無解，正常情況不會用到）
                    m.lb[T[(s.id, h)]] = lo
                    over = m.var(0.0, np.inf, UNMET_PENALTY)
                    m.add({T[(s.id, h)]: 1.0, over: -1.0}, -np.inf, hi)
                    cop = cop_override.get((s.id, h), L["cop"][h])   # 序列線性化：上一輪推演的實際 COP
                    q = m.var(0.0, L["capacity_w"][h], prices[h] / cop / 1000)
                    cooling[(s.id, h)] = q
                    terms[T[(s.id, h)]] += L["b"][h]
                    terms[q] = 1.0
                    m.add(terms, L["a"][h], L["a"][h])
                    ac_elec_terms[h][q] = 1.0 / cop
                    const_w[h] += L["latent_w"][h] / cop
                    m.c_const = getattr(m, "c_const", 0.0) + prices[h] * L["latent_w"][h] / cop / 1000
                    if not hourly_setpoints:   # 分組：同一組的小時室溫 = 共同設定值（＋睡眠曲線）
                        key = (s.id, opt._variable_key(schedule, ctx, h))
                        if key not in group_vars:
                            group_vars[key] = m.var(lo, hi)
                        offset = ctx.curve[h] if key[1][2] == "sleep" and p.follow_sleep_curve else 0.0
                        # 室溫 = 共同設定值；能力不足時可以高於設定值（軟限制，同上）
                        above = m.var(0.0, np.inf, UNMET_PENALTY)
                        m.add({T[(s.id, h)]: 1.0, group_vars[key]: -1.0, above: -1.0}, offset, offset)
                else:
                    terms[T[(s.id, h)]] += L["ua_free"][h]
                    rhs = L["ua_free"][h] * L["t_free"][h] + L["gain_free"][h]
                    m.add(terms, rhs, rhs)

                # 舒適懲罰（只計有冷氣的空間，與原本目標函數相同）
                if s.id not in ac_ids:
                    continue
                p = opt._mode_params(schedule, h)
                ideal_awake, ideal_sleep = opt._ideal(ctx, h, prefs)
                for people, ideal in ((ctx.awake[h], ideal_awake), (ctx.asleep[h], ideal_sleep)):
                    if people <= 0:
                        continue
                    weight = p.comfort_weight * people
                    segs = [m.var(0.0, width, weight * slope) for width, slope in SEGMENTS]
                    # 超出量 e = Σ 分段 ≥ |T − ideal| − deadband
                    m.add({T[(s.id, h)]: -1.0, **{v: 1.0 for v in segs}}, -ideal - p.deadband_c, np.inf)
                    m.add({T[(s.id, h)]: 1.0, **{v: 1.0 for v in segs}}, ideal - p.deadband_c, np.inf)

        # 可移轉負載：每個可行開始時間一個 0/1 變數
        tasks = lm.shiftable_tasks(weather, day_type)
        quiet = opt._quiet_hours(schedule, day_type)
        slots = [i * TASK_STEP_H for i in range(int(24 / TASK_STEP_H))]
        task_vars: dict[str, list[tuple[int, float, list[float]]]] = {}
        task_kw_terms = {h: {} for h in range(24)}
        for task in tasks:
            aid = task["appliance"]
            shift = opt._mode_params(schedule, int(task["default_start"]) % 24).shift_loads
            options = []
            for start in (slots if shift else [task["default_start"]]):
                placed = place_task(task, start)
                if not placed["feasible"]:
                    continue
                kw = [w / 1000 for w in task_hourly(placed, start)]
                if aid in NOISY_TASKS and shift and any(k > 0 and quiet[h] for h, k in enumerate(kw)):
                    continue
                gap = abs(start % 24 - task["default_start"]) % 24
                cost = sum(k * pr for k, pr in zip(kw, prices)) + 1e-4 * min(gap, 24 - gap)
                x = m.var(0, 1, cost, integer=True)
                options.append((x, start % 24, kw))
                for h, k in enumerate(kw):
                    if k:
                        task_kw_terms[h][x] = k
            if not options:                       # 沒有可行時間（不應發生）：維持原本習慣
                continue
            m.add({x: 1.0 for x, _, _ in options}, 1.0, 1.0)
            task_vars[aid] = options
        if "washer" in task_vars and "dryer" in task_vars:
            max_wait = lm.params["shiftable_tasks"]["dryer"]["max_wait_after_washer_h"]
            washer = next(t for t in tasks if t["appliance"] == "washer")
            for xw, ws, _ in task_vars["washer"]:
                end = ws + washer["duration_h"]
                allowed = {round((end + k * TASK_STEP_H) % 24, 2) for k in range(int(max_wait / TASK_STEP_H) + 1)}
                ok = [xd for xd, ds, _ in task_vars["dryer"] if round(ds, 2) in allowed]
                if len(task_vars["dryer"]) > 1:   # 烘衣可移動時才加限制
                    m.add({**{xd: 1.0 for xd in ok}, xw: -1.0}, 0.0, np.inf)

        # 全屋尖峰需量（kW）
        peak = m.var(0.0, np.inf, opt.demand_weight)
        for h in range(24):
            terms = {v: -c / 1000 for v, c in ac_elec_terms[h].items()}
            for v, k in task_kw_terms[h].items():
                terms[v] = terms.get(v, 0.0) - k
            terms[peak] = 1.0
            m.add(terms, const_w[h] / 1000, np.inf)

        res = m.solve(time_limit)
        if res.x is None:
            raise RuntimeError(f"MILP 求解失敗：{res.message}")
        x = res.x

        # 轉成控制策略：冷氣運轉的小時，設定溫度 = 模型室溫（取 0.5°C 間隔，限制在模式範圍內）
        for s in opt.ac_spaces:
            on = plan.ac_on[s.id]
            sp = []
            for h in range(24):
                lo, hi = opt._mode_params(schedule, h).setpoint_range_c
                value = round(x[T[(s.id, h)]] / SETPOINT_STEP_C) * SETPOINT_STEP_C
                sp.append(min(hi, max(lo, value)) if on[h] > 0 else prefs["preferred_awake_c"])
            plan.setpoints[s.id] = sp
        for aid, options in task_vars.items():
            chosen = max(options, key=lambda o: x[o[0]])
            plan.task_starts[aid] = chosen[1]

        return {"objective": res.fun + getattr(m, "c_const", 0.0), "variables": len(m.c), "binaries": sum(m.integer),
                "constraints": len(m.row_lo), "status": res.message}

    # 序列線性化（MPC 常用）：線性模型的 COP 固定，和實際變頻冷氣（低負載效率高）有落差；
    # 每輪用完整推演算出的實際 COP 代回重解，保留「完整推演評估」目標值最好的一輪
    best, cop_override, history = None, {}, []
    for _ in range(refine + 1):
        info = solve_once(cop_override)
        result = sim.run(day, plan_id, plan)
        score = opt.evaluate(result, schedule)["目標值(元)"]
        history.append(round(score, 2))
        if best is None or score < best[0] - 1e-9:
            best = (score, copy.deepcopy(plan), result, info)
        cli = result.climate
        new = {(r.space_id, int(r.hour)): float(r.cop) for r in cli.itertuples() if r.ac_on >= 0.5 and r.cop > 0}
        if new == cop_override:
            break
        cop_override = new
    _, plan, result, info = best
    info = {"method": "milp", "hourly_setpoints": hourly_setpoints, **info, "rounds": history,
            "solve_s": time.perf_counter() - t0}
    return OptimizationResult(schedule, plan, result, opt._setpoint_table(result), opt._task_table(result), info)
