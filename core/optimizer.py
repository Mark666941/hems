"""最佳化層：四種模式 = 同一套最佳化 + 不同參數（config/modes.json）。

目標函數（每個模式都一樣）：
    最小化  電費 + Σ 舒適權重 × 人數 × max(0, |室溫 − 偏好溫度| − 容許偏差)²

決策變數與解法：
1. 冷氣設定溫度：每個有冷氣的空間，依「模式時段 × 清醒/睡眠」各決定一個設定值，
   以座標下降法（coordinate descent）求解：輪流固定其他變數，在 0.5°C 間隔的候選值中找最好的。
   室溫有蓄熱，前後小時互相影響，所以每次都用負載模型推演整天來評估。
2. 冷氣開關：有人才開；室外低於模式門檻時開窗不開冷氣；效能模式在回家前預冷。
3. 可移轉負載（洗衣、烘衣、燒熱水）開始時間：以 0.5 小時為單位窮舉所有組合，
   在「熱水要在洗澡前燒好」「烘衣緊接洗衣」「睡眠時段不運轉吵人的家電」等限制下找最便宜的。
   組合數只有數千個，窮舉就能得到全域最佳解。
4. 其他家電：依模式調整係數（照明調光、除濕機關閉等）；必要負載永遠不動。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

import pandas as pd

from core.load_model import place_task, task_hourly
from core.models import LoadType, Space
from core.parallel import parallel_map
from core.schedule import day_type_for
from core.simulator import ControlPlan, SimulationResult, Simulator, load_baseline_plan

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
SETPOINT_STEP_C = 0.5
TASK_STEP_H = 0.5
MODE_ORDER = ["saving", "normal", "performance", "sleep"]
NOISY_TASKS = {"washer", "dryer"}   # 睡眠時段不宜運轉的家電


# ---------- 模式參數 ----------

@dataclass(frozen=True)
class ModeParams:
    setpoint_range_c: tuple[float, float]
    comfort_weight: float           # 元 / (人·°C²·h)：願意為舒適付多少電費
    deadband_c: float               # 偏離偏好溫度在此範圍內不算不舒適
    ac_min_outdoor_c: float         # 室外低於此溫度改開窗、不開冷氣
    precool_h: float                # 回家前提早開冷氣的時數
    sleep_aware: bool               # 睡眠時段是否另外決定設定溫度（一般習慣是整天同一個值）
    follow_sleep_curve: bool        # 睡眠時設定溫度是否跟著睡眠曲線調整
    appliance_factors: dict = field(default_factory=dict, hash=False)
    shift_loads: bool = True        # 可移轉負載是否移到便宜時段
    quiet_during_sleep: bool = True  # 睡眠時段不運轉洗衣機、烘衣機


@dataclass
class Mode:
    id: str
    name: str
    description: str
    params: ModeParams
    night: ModeParams | None = None
    night_hours: tuple[int, int] | None = None

    def params_at(self, hour: int) -> ModeParams:
        if self.night is not None and _in_window(hour, *self.night_hours):
            return self.night
        return self.params

    def segment_at(self, hour: int) -> str:
        return "night" if self.night is not None and _in_window(hour, *self.night_hours) else "day"


def _in_window(hour: int, start: int, end: int) -> bool:
    """hour 是否在 [start, end) 內；可跨午夜，例如 23–7。"""
    return start <= hour < end if start <= end else (hour >= start or hour < end)


def _to_params(raw: dict) -> ModeParams:
    raw = dict(raw)
    raw["setpoint_range_c"] = tuple(raw["setpoint_range_c"])
    return ModeParams(**raw)


def load_modes(path: Path = CONFIG_DIR / "modes.json", cfg: dict | None = None) -> tuple[dict, dict[str, Mode], float]:
    """讀取 modes.json（或直接給設定字典 cfg），回傳（舒適偏好, {模式 id: Mode}, 可移轉負載的需量懲罰 元/kW）。"""
    if cfg is None:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    modes = {}
    for mid, m in cfg["modes"].items():
        params = _to_params(m["params"])
        night = None
        if "night" in m:
            overrides = dict(m["night"])
            if "setpoint_range_c" in overrides:
                overrides["setpoint_range_c"] = tuple(overrides["setpoint_range_c"])
            night = replace(params, **overrides)
        modes[mid] = Mode(mid, m["name"], m["description"], params, night,
                          tuple(m["night_hours"]) if "night_hours" in m else None)
    return cfg["comfort"], modes, cfg.get("task_demand_weight_per_kw", 0.0)


@dataclass
class ModeSchedule:
    """每小時使用哪個模式，例如白天正常、23:00 自動切舒眠。單一模式時 24 小時都相同。"""

    by_hour: list[str]
    name: str

    @classmethod
    def single(cls, mode: Mode) -> ModeSchedule:
        return cls([mode.id] * 24, mode.name)

    @classmethod
    def switch(cls, modes: dict[str, Mode], day_mode: str, night_mode: str,
               night_start: int = 23, night_end: int = 7) -> ModeSchedule:
        by_hour = [night_mode if _in_window(h, night_start, night_end) else day_mode for h in range(24)]
        name = f"{modes[day_mode].name} → {night_start:02d}:00 {modes[night_mode].name}"
        return cls(by_hour, name)


# ---------- 舒適度 ----------

@dataclass
class ComfortContext:
    """某空間某日的人員狀態，用來計算不舒適度。"""

    awake: list[float]        # 每小時清醒人·時
    asleep: list[float]       # 每小時睡眠人·時
    is_sleep: list[bool]      # 該小時是否以睡眠為主
    curve: list[float]        # 睡眠曲線：入睡後可接受溫度上升量（°C）


def sleep_curve(is_sleep: list[bool], rise_per_h: float, max_rise: float, return_before_wake_h: int) -> list[float]:
    """入睡後每小時升 rise_per_h，最多 max_rise；起床前 return_before_wake_h 小時回到 0。"""
    n = len(is_sleep)
    curve = [0.0] * n
    starts = [h for h in range(n) if is_sleep[h] and not is_sleep[(h - 1) % n]]
    for start in starts:
        length = 0
        while is_sleep[(start + length) % n] and length < n:
            length += 1
        for k in range(length):
            if k < length - return_before_wake_h:
                curve[(start + k) % n] = min(max_rise, rise_per_h * k)
    return curve


def discomfort(temp: float, ideal: float, deadband: float) -> float:
    """偏離偏好溫度超過容許範圍的度數（°C）。"""
    return max(0.0, abs(temp - ideal) - deadband)


# ---------- 最佳化結果 ----------

@dataclass
class OptimizationResult:
    schedule: ModeSchedule
    plan: ControlPlan
    result: SimulationResult
    setpoints: pd.DataFrame     # 各空間每小時：設定溫度、冷氣運轉比例、室溫
    tasks: pd.DataFrame         # 可移轉工作：原本時間 → 最佳化後時間
    levels: dict                # 各空間決策變數的最佳值

    @property
    def name(self) -> str:
        return self.schedule.name


class Optimizer:
    def __init__(self, simulator: Simulator | None = None, modes_path: Path = CONFIG_DIR / "modes.json",
                 modes_config: dict | None = None, baseline: ControlPlan | None = None):
        self.sim = simulator or Simulator()
        self.comfort, self.modes, self.demand_weight = load_modes(modes_path, modes_config)
        self.baseline_plan = baseline or load_baseline_plan()   # 比較基準（一般習慣）
        self._ctx_cache: dict[tuple, dict[str, ComfortContext]] = {}

    # ----- 共用資料 -----

    @property
    def ac_spaces(self) -> list[Space]:
        return [s for s in self.sim.house.spaces
                if (a := s.get_appliance("air_conditioner")) is not None and a.enabled]

    def season(self, day: date) -> tuple[str, dict]:
        """依月份取得季節的舒適偏好（冷房季節 / 非冷房季節衣著不同）。"""
        for name, prefs in self.comfort["seasons"].items():
            if day.month in prefs["months"]:
                return name, prefs
        raise ValueError(f"modes.json 的 comfort.seasons 沒有涵蓋 {day.month} 月")

    def contexts(self, day_type: str, season: str) -> dict[str, ComfortContext]:
        key = (day_type, season)
        if key not in self._ctx_cache:
            sched = self.sim.schedule
            total = sched.occupancy(day_type)
            asleep = sched.occupancy(day_type, activity="sleep")
            c = self.comfort["seasons"][season].get("sleep_curve")
            ctx = {}
            for s in self.sim.house.spaces:
                if s.area_ping <= 0:
                    continue
                is_sleep = sched.sleep_hours(day_type, s.id)
                curve = (sleep_curve(is_sleep, c["rise_c_per_h"], c["max_rise_c"], c["return_before_wake_h"])
                         if c else [0.0] * 24)
                ctx[s.id] = ComfortContext(awake=list(total[s.id] - asleep[s.id]), asleep=list(asleep[s.id]),
                                           is_sleep=is_sleep, curve=curve)
            self._ctx_cache[key] = ctx
        return self._ctx_cache[key]

    @staticmethod
    def _ideal(ctx: ComfortContext, h: int, prefs: dict) -> tuple[float, float]:
        """（清醒偏好溫度, 睡眠偏好溫度含睡眠曲線）"""
        return prefs["preferred_awake_c"], prefs["preferred_sleep_c"] + ctx.curve[h]

    def _mode_params(self, schedule: ModeSchedule, h: int) -> ModeParams:
        return self.modes[schedule.by_hour[h]].params_at(h)

    # ----- 1. 冷氣開關 -----

    @staticmethod
    def occupied_blocks(presence: list[float]) -> list[list[int]]:
        """把「有人」的小時分成連續時段（可跨午夜），例如 [[22, 23, 0, …, 7], [16, 17, 18]]。"""
        n = len(presence)
        if all(p > 0 for p in presence):
            return [list(range(n))]
        blocks = []
        for start in range(n):
            if presence[start] > 0 and presence[(start - 1) % n] == 0:
                block, h = [], start
                while presence[h % n] > 0:
                    block.append(h % n)
                    h += 1
                blocks.append(block)
        return blocks

    def _ac_on(self, space: Space, schedule: ModeSchedule, day_type: str, temps: list[float]) -> list[float]:
        """有人才開冷氣。以「連續有人的時段」整段判斷：該時段室外平均溫度低於模式門檻就開窗不開，
        避免室外溫度在門檻附近時每小時開開關關。效能模式在每個時段開始前預冷。"""
        presence = self.sim.schedule.presence(day_type, space.id)
        on = [0.0] * 24
        for block in self.occupied_blocks(presence):
            mean_temp = sum(temps[h] for h in block) / len(block)
            if mean_temp < self._mode_params(schedule, block[0]).ac_min_outdoor_c:
                continue
            for h in block:
                on[h] = presence[h]
            first = block[0]
            precool = self._mode_params(schedule, first).precool_h
            if precool > 0:
                on[first] = min(1.0, presence[first] + precool)          # 到家那小時提早開
                if presence[first] + precool > 1:
                    before = (first - 1) % 24
                    on[before] = max(on[before], min(1.0, presence[first] + precool - 1))
        return on

    # ----- 2. 家電調整係數 -----

    def _appliance_factors(self, space: Space, schedule: ModeSchedule) -> dict[str, list[float]]:
        factors = {}
        for a in space.appliances:
            if a.load_type is LoadType.ESSENTIAL or a.id == "air_conditioner":
                continue
            values = [self._mode_params(schedule, h).appliance_factors.get(a.id, 1.0) for h in range(24)]
            if any(v != 1.0 for v in values):
                factors[a.id] = values
        return factors

    # ----- 3. 冷氣設定溫度（座標下降） -----

    def _variable_key(self, schedule: ModeSchedule, ctx: ComfortContext, h: int) -> tuple[str, str, str]:
        """決策變數的分組：同一組的小時共用一個設定溫度。
        sleep_aware 的模式把睡眠時段分開決定；否則整天一個值（一般人的習慣）。"""
        mode = self.modes[schedule.by_hour[h]]
        sleeping = ctx.is_sleep[h] and mode.params_at(h).sleep_aware
        return mode.id, mode.segment_at(h), "sleep" if sleeping else "awake"

    def _setpoints_from_levels(self, levels: dict, schedule: ModeSchedule, ctx: ComfortContext) -> list[float]:
        result = []
        for h in range(24):
            key = self._variable_key(schedule, ctx, h)
            p = self._mode_params(schedule, h)
            value = levels.get(key, p.setpoint_range_c[0])
            if key[2] == "sleep" and p.follow_sleep_curve:
                value += ctx.curve[h]
            result.append(min(p.setpoint_range_c[1], value))
        return result

    def _space_objective(self, space, weather, day_type, prices, setpoints, ac_on, factors,
                         ctx: ComfortContext, schedule: ModeSchedule, prefs: dict,
                         neighbor_temps: dict | None = None) -> tuple[float, dict]:
        df = self.sim.load_model.space_hourly(space, weather, day_type, setpoints, ac_on, factors,
                                              neighbor_temps=neighbor_temps, as_frame=False)
        cost = sum(w / 1000 * price for w, price in zip(df["air_conditioner"], prices))
        penalty = 0.0
        for h, temp in enumerate(df["room_temp_c"]):
            p = self._mode_params(schedule, h)
            ideal_awake, ideal_sleep = self._ideal(ctx, h, prefs)
            penalty += p.comfort_weight * (
                ctx.awake[h] * discomfort(temp, ideal_awake, p.deadband_c) ** 2
                + ctx.asleep[h] * discomfort(temp, ideal_sleep, p.deadband_c) ** 2
            )
        return cost + penalty, df

    def _optimize_space(self, space, weather, day_type, prices, schedule, ac_on, factors,
                        season: str, neighbor_temps: dict | None = None) -> tuple[dict, list[float]]:
        prefs = self.comfort["seasons"][season]
        ctx = self.contexts(day_type, season)[space.id]
        keys = sorted({self._variable_key(schedule, ctx, h) for h in range(24) if ac_on[h] > 0})
        if not keys:
            return {}, [prefs["preferred_awake_c"]] * 24

        def candidates(key) -> list[float]:
            mode = self.modes[key[0]]
            lo, hi = (mode.night if key[1] == "night" else mode.params).setpoint_range_c
            n = int(round((hi - lo) / SETPOINT_STEP_C))
            return [lo + i * SETPOINT_STEP_C for i in range(n + 1)]

        def preferred(key) -> float:
            target = prefs["preferred_sleep_c" if key[2] == "sleep" else "preferred_awake_c"]
            options = candidates(key)
            return min(options, key=lambda v: abs(v - target))

        def evaluate(levels) -> float:
            sp = self._setpoints_from_levels(levels, schedule, ctx)
            return self._space_objective(space, weather, day_type, prices, sp, ac_on, factors,
                                         ctx, schedule, prefs, neighbor_temps)[0]

        levels = {k: preferred(k) for k in keys}
        best = evaluate(levels)
        for _ in range(3):                      # 座標下降：最多三輪，沒有改善就停
            improved = False
            for key in keys:
                for value in candidates(key):
                    if value == levels[key]:
                        continue
                    trial = {**levels, key: value}
                    score = evaluate(trial)
                    if score < best - 1e-9:
                        best, levels, improved = score, trial, True
            if not improved:
                break
        return levels, self._setpoints_from_levels(levels, schedule, ctx)

    # ----- 4. 可移轉負載（窮舉） -----

    def _quiet_hours(self, schedule: ModeSchedule, day_type: str) -> list[bool]:
        sleeping = self.sim.schedule.occupancy(day_type, activity="sleep").sum(axis=1)
        return [bool(sleeping[h] >= 1 and self._mode_params(schedule, h).quiet_during_sleep) for h in range(24)]

    def _optimize_tasks(self, tasks: list[dict], prices: list[float], quiet: list[bool],
                        schedule: ModeSchedule) -> dict[str, float]:
        """熱水器 × 洗衣機 × 烘衣機的開始時間一起窮舉（0.5 小時一格，約一萬種組合）。

        分數 = 電費 + 需量懲罰 × 三項工作同時運轉的最大 kW + 極小的移動量（電費相同時少移動）
        限制：熱水在第一個人洗澡前燒好；烘衣在洗衣結束後 max_wait 小時內開始；
              模式要求安靜時，睡眠時段不運轉洗衣機、烘衣機。
        """
        slots = [i * TASK_STEP_H for i in range(int(24 / TASK_STEP_H))]
        by_id = {t["appliance"]: t for t in tasks}
        zero = [0.0] * 24
        none_option = [(None, zero, 0.0)]

        def shiftable(task) -> bool:
            # 依「原本習慣開始時間」那一刻的模式決定要不要移轉
            return self._mode_params(schedule, int(task["default_start"]) % 24).shift_loads

        def candidate_starts(task) -> list[float]:
            return slots if shiftable(task) else [task["default_start"]]

        def options_for(task, starts) -> list[tuple[float, list[float], float]]:
            """可行的（開始時間, 每小時 kW, 電費 + 移動懲罰）。"""
            result = []
            for s in starts:
                placed = place_task(task, s)
                if not placed["feasible"]:
                    continue
                hourly_kw = [w / 1000 for w in task_hourly(placed, s)]
                if task["appliance"] in NOISY_TASKS and any(kw > 0 and quiet[h] for h, kw in enumerate(hourly_kw)):
                    continue
                gap = abs(s % 24 - task["default_start"]) % 24
                moved = min(gap, 24 - gap)
                cost = sum(kw * p for kw, p in zip(hourly_kw, prices)) + 1e-4 * moved
                result.append((s % 24, hourly_kw, cost))
            return result

        heater, washer, dryer = by_id.get("water_heater"), by_id.get("washer"), by_id.get("dryer")
        heater_opts = options_for(heater, candidate_starts(heater)) if heater else none_option
        washer_opts = options_for(washer, candidate_starts(washer)) if washer else none_option
        dryer_opts: dict = {}
        if dryer:
            max_wait = self.sim.load_model.params["shiftable_tasks"]["dryer"]["max_wait_after_washer_h"]
            for w_start, _, _ in washer_opts:
                if w_start is not None and shiftable(washer):
                    end = w_start + washer["duration_h"]
                    starts = [(end + k * TASK_STEP_H) % 24 for k in range(int(max_wait / TASK_STEP_H) + 1)]
                else:
                    starts = [dryer["default_start"]]
                dryer_opts[w_start] = options_for(dryer, starts)

        best = None
        for h_start, h_kw, h_cost in heater_opts:
            for w_start, w_kw, w_cost in washer_opts:
                for d_start, d_kw, d_cost in (dryer_opts.get(w_start, []) if dryer else none_option):
                    peak_kw = max(a + b + c for a, b, c in zip(h_kw, w_kw, d_kw))
                    score = h_cost + w_cost + d_cost + self.demand_weight * peak_kw
                    if best is None or score < best[0]:
                        best = (score, h_start, w_start, d_start)
        if best is None:
            return {}   # 沒有可行組合（不應發生），維持原本習慣
        return {aid: s for aid, s in zip(("water_heater", "washer", "dryer"), best[1:]) if s is not None}

    # ----- 主流程 -----

    def optimize(self, day: date, mode: str | ModeSchedule, plan_id: str = "tou_2",
                 method: str = "coordinate") -> OptimizationResult:
        """method："coordinate"＝逐房座標下降＋可移轉負載窮舉（預設）；"milp"＝全屋混合整數規劃（core/milp.py）；
        "hybrid"＝MILP 解再於完整推演上逐時局部搜尋。"""
        if method == "milp":
            from core.milp import optimize_milp  # scipy 較慢載入，需要時才匯入
            return optimize_milp(self, day, mode, plan_id)
        if method == "hybrid":
            from core.milp import optimize_hybrid
            return optimize_hybrid(self, day, mode, plan_id)
        schedule = mode if isinstance(mode, ModeSchedule) else ModeSchedule.single(self.modes[mode])
        weather = self.sim.weather.get_hourly_weather(day)
        weather = self.sim.load_model.with_solar_geometry(weather, day)   # 各方位牆面日射
        day_type = day_type_for(day, self.sim.tariff.is_off_peak_day)
        prices = list(self.sim.tariff.hourly_prices(day, plan_id)["price"])
        temps = list(weather["temp_c"])
        season, prefs = self.season(day)

        plan = ControlPlan(name=schedule.name, default_setpoint=prefs["preferred_awake_c"])
        levels_all = {}
        for space in self.sim.house.spaces:
            factors = self._appliance_factors(space, schedule)
            if factors:
                plan.appliance_factors[space.id] = factors
        for space in self.ac_spaces:
            plan.ac_on[space.id] = self._ac_on(space, schedule, day_type, temps)
        # 房間之間有隔間熱交換：先以偏好溫度推演一次得到各房室溫，逐房最佳化時把鄰房室溫當已知條件
        neighbor_temps = None
        if self.sim.load_model.interzone_ua:
            frames = self.sim.run_spaces(weather, day_type, plan)
            neighbor_temps = {sid: list(df["room_temp_c"]) for sid, df in frames.items()
                              if self.sim.house.get_space(sid).area_ping > 0}
        for space in self.ac_spaces:
            ac_on = plan.ac_on[space.id]
            levels, setpoints = self._optimize_space(space, weather, day_type, prices, schedule, ac_on,
                                                     plan.appliance_factors.get(space.id), season, neighbor_temps)
            plan.setpoints[space.id] = setpoints
            levels_all[space.id] = levels

        tasks = self.sim.load_model.shiftable_tasks(weather, day_type)
        plan.task_starts = self._optimize_tasks(tasks, prices, self._quiet_hours(schedule, day_type), schedule)

        result = self.sim.run(day, plan_id, plan)
        return OptimizationResult(schedule, plan, result, self._setpoint_table(result),
                                  self._task_table(result), levels_all)

    def evaluate(self, result: SimulationResult, schedule: ModeSchedule) -> dict[str, float]:
        """以同一套標準評估任一控制策略的推演結果（比較不同最佳化方法用）：
        全屋流動電費、舒適懲罰（與目標函數相同的平方項）、全屋最大用電（kW），目標值 = 三者加總（需量 × 權重）。"""
        season, prefs = self.season(result.day)
        ctx_all = self.contexts(result.day_type, season)
        by_space = {sid: list(g.sort_values("hour")["room_temp_c"]) for sid, g in result.climate.groupby("space_id")}
        penalty = 0.0
        for space in self.ac_spaces:
            ctx, temps = ctx_all[space.id], by_space[space.id]
            for h, temp in enumerate(temps):
                p = self._mode_params(schedule, h)
                ideal_awake, ideal_sleep = self._ideal(ctx, h, prefs)
                penalty += p.comfort_weight * (ctx.awake[h] * discomfort(temp, ideal_awake, p.deadband_c) ** 2
                                               + ctx.asleep[h] * discomfort(temp, ideal_sleep, p.deadband_c) ** 2)
        cost, peak = result.energy_cost, result.peak_demand_kw
        return {"電費(元)": cost, "舒適懲罰(元)": penalty, "尖峰需量(kW)": peak,
                "目標值(元)": cost + penalty + self.demand_weight * peak}

    @staticmethod
    def _setpoint_table(result: SimulationResult) -> pd.DataFrame:
        c = result.climate
        return c[c["ac_on"] > 0][["space", "hour", "setpoint_c", "ac_on", "room_temp_c"]].reset_index(drop=True)

    @staticmethod
    def _task_table(result: SimulationResult) -> pd.DataFrame:
        return pd.DataFrame([{
            "家電": t["name"], "原本開始": t["default_start"], "最佳化開始": t["start"],
            "運轉(h)": round(t["duration_h"], 2), "耗電(kWh)": round(t["energy_kwh"], 2),
        } for t in result.tasks])

    # ----- 舒適度評估（各模式共同基準）-----

    def comfort_score(self, result: SimulationResult) -> dict[str, float]:
        """以使用者偏好溫度 ± evaluation_deadband_c 為共同基準，評估有冷氣空間的舒適度。
        回傳：
        - 不舒適度（人·°C·h）：超出舒適範圍的度數 × 人數 × 時數，越小越好
        - 舒適時間比例（%）：在舒適範圍內的人·時比例
        - 平均溫差（°C）：室溫與偏好溫度（含睡眠曲線）的平均差距，連續指標，可比較範圍內的細微差異
        """
        season, prefs = self.season(result.day)
        ctx_all = self.contexts(result.day_type, season)
        deadband = self.comfort["evaluation_deadband_c"]
        degree_hours = person_hours = comfortable = abs_diff = 0.0
        for space in self.ac_spaces:
            ctx = ctx_all[space.id]
            temps = result.climate[result.climate["space_id"] == space.id]["room_temp_c"].tolist()
            for h, temp in enumerate(temps):
                ideal_awake, ideal_sleep = self._ideal(ctx, h, prefs)
                for people, ideal in ((ctx.awake[h], ideal_awake), (ctx.asleep[h], ideal_sleep)):
                    if people <= 0:
                        continue
                    d = discomfort(temp, ideal, deadband)
                    degree_hours += people * d
                    person_hours += people
                    comfortable += people * (d == 0)
                    abs_diff += people * abs(temp - ideal)
        return {"不舒適度(人·°C·h)": degree_hours,
                "舒適時間比例(%)": 100 * comfortable / person_hours if person_hours else 100.0,
                "平均溫差(°C)": abs_diff / person_hours if person_hours else 0.0}

    def compare(self, day: date, plan_id: str = "tou_2") -> tuple[pd.DataFrame, dict]:
        """基準情境與四種模式的比較。回傳（比較表, {名稱: SimulationResult 或 OptimizationResult}）。"""
        results: dict = {"基準": self.sim.run(day, plan_id, self.baseline_plan)}
        for mid in MODE_ORDER:
            results[mid] = self.optimize(day, mid, plan_id)
        rows = []
        base = results["基準"]
        for key, r in results.items():
            sim_result = r if isinstance(r, SimulationResult) else r.result
            name = "基準（一般習慣）" if key == "基準" else self.modes[key].name
            periods = sim_result.kwh_by_period()
            rows.append({
                "模式": name, "用電(kWh)": sim_result.total_kwh, "流動電費(元)": sim_result.energy_cost,
                "尖峰用電(kWh)": periods["尖峰"] + periods["半尖峰"], "最大需量(kW)": sim_result.peak_demand_kw,
                "冷氣用電(kWh)": float(sim_result.by_category()["冷氣"]),
                **self.comfort_score(sim_result),
                "電費節省(%)": 100 * (1 - sim_result.energy_cost / base.energy_cost) if base.energy_cost else 0.0,
            })
        return pd.DataFrame(rows), results

    def compare_month(self, year: int, month: int, plan_id: str = "tou_2") -> tuple[pd.DataFrame, dict]:
        """整月：基準與四種模式（每天各自最佳化）的月用電、月電費，並比較三種電價方案。
        模式依 plan_id 的電價最佳化；同一份用電也換算成其他方案的電費，看值不值得換方案。
        回傳（比較表, {模式名稱: 每日 SimulationResult 列表}）。"""
        import calendar

        from core.simulator import MonthResult

        days = [date(year, month, d) for d in range(1, calendar.monthrange(year, month)[1] + 1)]
        per_day = parallel_map(_month_day, days, self, plan_id)   # 每天分給不同 CPU 核心（D3）
        names = ["基準（一般習慣）", *(self.modes[mid].name for mid in MODE_ORDER)]
        scenarios = {name: [results[i] for results in per_day] for i, name in enumerate(names)}

        rows = []
        for name, day_results in scenarios.items():
            month_result = MonthResult(self.sim, day_results)
            bills = month_result.bills().set_index("plan_id")["月電費"]
            scores = [self.comfort_score(r) for r in day_results]
            rows.append({
                "模式": name, "月用電(kWh)": month_result.total_kwh,
                **{f"{self.sim.tariff.plans[p]['name']}(元)": bills[p] for p in self.sim.tariff.plans},
                "平均舒適時間比例(%)": sum(s["舒適時間比例(%)"] for s in scores) / len(scores),
                "平均溫差(°C)": sum(s["平均溫差(°C)"] for s in scores) / len(scores),
            })
        return pd.DataFrame(rows), scenarios


def _month_day(day: date, opt: Optimizer, plan_id: str) -> list[SimulationResult]:
    """整月分析的一天：基準與四種模式的推演結果（模組層級函式，才能交給其他程序平行計算）。"""
    return [opt.sim.run(day, plan_id, opt.baseline_plan),
            *(opt.optimize(day, mid, plan_id).result for mid in MODE_ORDER)]


# ---------- 命令列 ----------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="HEMS 四種模式最佳化與比較")
    parser.add_argument("day", nargs="?", help="日期，例如 2026-07-20；省略時跑夏、冬代表日")
    parser.add_argument("--month", help="整月比較，例如 2026-07（約 3 秒，多核心平行）")
    parser.add_argument("--plan", default="tou_2", help="電價方案：tou_2 / tou_3")
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args()

    pd.set_option("display.unicode.east_asian_width", True)
    pd.set_option("display.width", 220)
    out_dir = Path(__file__).resolve().parent.parent / "output"
    out_dir.mkdir(exist_ok=True)
    opt = Optimizer()

    if args.month:
        year, month = map(int, args.month.split("-"))
        table, _ = opt.compare_month(year, month, args.plan)
        print(f"\n=== {args.month} 整月模式比較（依 {opt.sim.tariff.plans[args.plan]['name']} 最佳化）===")
        print(table.round(1).to_string(index=False))
        table.to_csv(out_dir / f"modes_month_{args.month}.csv", index=False, encoding="utf-8-sig")
        return

    if args.day:
        days = [date.fromisoformat(args.day)]
    else:
        from core.data_source import load_representative_days
        days = list(load_representative_days().values())

    for day in days:
        table, results = opt.compare(day, args.plan)
        day_name = "平日" if results["基準"].day_type == "weekday" else "假日"
        print(f"\n{'=' * 80}\n{day}（{day_name}）四種模式比較｜{opt.sim.tariff.plans[args.plan]['name']}\n{'=' * 80}")
        print(table.round(2).to_string(index=False))

        for mid in MODE_ORDER:
            r: OptimizationResult = results[mid]
            print(f"\n--- {r.name} ---")
            for space in opt.ac_spaces:
                sp = r.setpoints[r.setpoints["space"] == space.name]
                if sp.empty:
                    continue
                hours = ", ".join(f"{int(row.hour):02d}時 {row.setpoint_c:.1f}" for row in sp.itertuples())
                print(f"  {space.name} 冷氣設定：{hours}")
            for _, row in r.tasks.iterrows():
                print(f"  {row['家電']}：{row['原本開始']:05.2f} → {row['最佳化開始']:05.2f} 時，"
                      f"運轉 {row['運轉(h)']:.2f} h，{row['耗電(kWh)']:.2f} kWh")

        table.to_csv(out_dir / f"modes_{day.isoformat()}.csv", index=False, encoding="utf-8-sig")
        if not args.no_plot:
            from core.charts import plot_modes
            print(f"\n圖檔：{plot_modes(table, results, out_dir / f'modes_{day.isoformat()}.png')}")


if __name__ == "__main__":
    main()
