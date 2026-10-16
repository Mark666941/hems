"""負載與耗電模型：給定空間、天氣、時間、設定溫度，算出每小時各家電耗電。

冷房負載（W）＝ 外牆傳熱 + 窗戶傳熱 + 窗戶日射 + 人體（顯熱＋潛熱）
             + 室內家電發熱 + 外氣滲透（顯熱＋潛熱）+ 蓄熱移除（pull-down）
冷氣耗電（W）＝ 冷房負載 ÷ COP，COP 隨室外溫度、設定溫度、部分負載率變化。

簡化假設（之後可精進）：
- 外牆用「等效日射溫度（sol-air）＋ 時滯與衰減」近似牆體蓄熱，假設每天天氣週期性重複。
- 中間樓層，無屋頂、地板熱傳；隔間牆兩側同溫不計。
- 室內蓄熱以單一熱容量近似，只在冷氣開機或調降設定溫度時計入。
- 只模擬冷房；冬季暖房由浴室暖風機負責，冷氣不開暖氣。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields
from pathlib import Path

import pandas as pd

from core.models import Appliance, House, Space
from core.schedule import Schedule

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
MJ_PER_HOUR_TO_W = 1e6 / 3600        # 每小時日射量 MJ/m² → 平均日射強度 W/m²
NOT_AWAKE = {"sleep", "out"}


# ---------- 濕空氣性質 ----------

def saturation_pressure_kpa(t_c: float) -> float:
    """飽和水蒸氣壓（Magnus 公式，Alduchov & Eskridge 1996 係數）。"""
    return 0.61094 * math.exp(17.625 * t_c / (t_c + 243.04))


def humidity_ratio(t_c: float, rh_pct: float, p_kpa: float = 101.325) -> float:
    """絕對濕度（kg 水 / kg 乾空氣）。"""
    pw = rh_pct / 100 * saturation_pressure_kpa(t_c)
    return 0.622 * pw / (p_kpa - pw)


# ---------- 空間幾何 ----------

@dataclass
class SpaceGeometry:
    floor_m2: float
    volume_m3: float
    wall_m2: float      # 外牆（不含窗）
    window_m2: float
    daylight: bool

    @classmethod
    def from_space(cls, space: Space, params: dict) -> "SpaceGeometry":
        g = params["geometry"]
        t = g["by_template"][space.template]
        floor = space.area_ping * g["ping_to_m2"]
        exterior = floor * t["exterior_wall_ratio"]
        window = exterior * t["window_ratio"]
        return cls(
            floor_m2=floor,
            volume_m3=floor * g["ceiling_height_m"],
            wall_m2=exterior - window,
            window_m2=window,
            daylight=t["daylight"],
        )

    def conductance_w_k(self, params: dict, ach: float) -> float:
        """室內外每差 1°C 的總熱傳量（W/K）：外牆 + 窗戶 + 換氣。"""
        env, inf = params["envelope"], params["infiltration"]
        ventilation = inf["air_density"] * inf["cp_air"] * self.volume_m3 * ach / 3600
        return env["u_wall"] * self.wall_m2 + env["u_window"] * self.window_m2 + ventilation


# ---------- 冷房負載 ----------

@dataclass
class CoolingLoad:
    """某小時的冷房負載組成（W，一小時平均）。"""

    wall: float = 0.0
    window_conduction: float = 0.0
    window_solar: float = 0.0
    people_sensible: float = 0.0
    people_latent: float = 0.0
    internal: float = 0.0
    infiltration_sensible: float = 0.0
    infiltration_latent: float = 0.0
    pulldown: float = 0.0

    @property
    def latent(self) -> float:
        # 冷氣只會除濕不會加濕，潛熱合計不小於 0
        return max(0.0, self.people_latent + self.infiltration_latent)

    @property
    def sensible(self) -> float:
        return (self.wall + self.window_conduction + self.window_solar + self.people_sensible
                + self.internal + self.infiltration_sensible + self.pulldown)

    @property
    def total(self) -> float:
        return max(0.0, self.sensible + self.latent)

    def as_dict(self) -> dict[str, float]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def sol_air_temps(temps: list[float], solar_w_m2: list[float], env: dict) -> list[float]:
    """等效日射溫度：外牆吸收日射後，相當於室外溫度升高多少。"""
    return [t + env["wall_absorptance"] * s * env["vertical_irradiance_ratio"] / env["h_out"]
            for t, s in zip(temps, solar_w_m2)]


def effective_wall_temps(sol_air: list[float], lag_h: int, decrement: float) -> list[float]:
    """牆體蓄熱：室內側感受到的是「數小時前」且「振幅衰減」的等效溫度。
    T_e(h) = 日平均 + 衰減係數 ×（T_sa(h − 時滯) − 日平均），假設天氣每天週期性重複。"""
    mean = sum(sol_air) / len(sol_air)
    n = len(sol_air)
    return [mean + decrement * (sol_air[(h - lag_h) % n] - mean) for h in range(n)]


def cooling_load(
    geo: SpaceGeometry,
    params: dict,
    t_out: float,
    rh_out: float,
    solar_w_m2: float,
    t_wall_eff: float,
    t_set: float,
    people_sensible_w: float = 0.0,
    people_latent_w: float = 0.0,
    internal_w: float = 0.0,
    on_fraction: float = 1.0,
    pulldown_w: float = 0.0,
) -> CoolingLoad:
    """計算某空間某小時的冷房負載。on_fraction 為冷氣這小時運轉的時間比例；
    人體與家電發熱已是一小時平均值，不再乘比例。"""
    env, inf = params["envelope"], params["infiltration"]
    airflow_m3_s = geo.volume_m3 * inf["ach"] / 3600
    w_out = humidity_ratio(t_out, rh_out)
    w_in = humidity_ratio(t_set, inf["indoor_rh_pct"])
    f = on_fraction
    return CoolingLoad(
        wall=f * env["u_wall"] * geo.wall_m2 * (t_wall_eff - t_set),
        window_conduction=f * env["u_window"] * geo.window_m2 * (t_out - t_set),
        window_solar=f * geo.window_m2 * solar_w_m2 * env["vertical_irradiance_ratio"]
        * env["shgc"] * env["interior_shading"],
        people_sensible=people_sensible_w,
        people_latent=people_latent_w,
        internal=internal_w,
        infiltration_sensible=f * inf["air_density"] * inf["cp_air"] * airflow_m3_s * (t_out - t_set),
        infiltration_latent=f * inf["air_density"] * airflow_m3_s * inf["h_fg"] * (w_out - w_in),
        pulldown=pulldown_w,
    )


# ---------- 冷氣效率 ----------

def ac_cop(t_out: float, t_set: float, part_load: float, ac: dict) -> float:
    """冷氣 COP：額定值 × 溫度修正 × 部分負載修正。"""
    temp_factor = (1 - ac["cop_outdoor_coef"] * (t_out - ac["rated_outdoor_c"])
                   + ac["cop_indoor_coef"] * (t_set - ac["rated_indoor_c"]))
    plr_min = ac["min_part_load"]
    if part_load >= plr_min:
        # 變頻機降頻運轉時效率較高
        plf = 1 + ac["part_load_gain"] * (1 - part_load)
    else:
        # 低於最低轉速，壓縮機開開停停，效率下降
        plf_min = 1 + ac["part_load_gain"] * (1 - plr_min)
        plf = plf_min * (1 - ac["cycling_degradation"] * (1 - part_load / plr_min))
    return max(ac["min_cop"], ac["rated_cop"] * temp_factor * plf)


@dataclass
class ACResult:
    power_w: float       # 耗電（W，一小時平均 = 該小時 Wh）
    delivered_w: float   # 實際移除的熱量
    unmet_w: float       # 能力不足、未移除的負載
    cop: float
    part_load: float


def ac_power(load_w: float, rated_power_w: float, t_out: float, t_set: float,
             ac: dict, on_fraction: float = 1.0) -> ACResult:
    """冷氣耗電 = 冷房負載 ÷ COP；額定冷房能力 = 額定耗電 × 額定 COP。"""
    if load_w <= 0 or on_fraction <= 0:
        return ACResult(0.0, 0.0, 0.0, 0.0, 0.0)
    capacity = rated_power_w * ac["rated_cop"] * on_fraction
    delivered = min(load_w, capacity)
    part_load = delivered / capacity
    cop = ac_cop(t_out, t_set, part_load, ac)
    return ACResult(delivered / cop, delivered, load_w - delivered, cop, part_load)


# ---------- 整合：空間逐時耗電 ----------

def load_params(path: Path = CONFIG_DIR / "load_params.json") -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class LoadModel:
    """結合房屋、作息、參數，計算各空間 24 小時耗電。"""

    def __init__(self, house: House, schedule: Schedule, params: dict | None = None):
        self.house = house
        self.schedule = schedule
        self.params = params or load_params()
        self.awake = set(schedule.activities) - NOT_AWAKE

    # ----- 人體發熱 -----

    def people_gains(self, space: Space, day_type: str) -> tuple[list[float], list[float]]:
        """每小時人體顯熱、潛熱（W）。"""
        p = self.params["people"]
        log = self.schedule.activity_log(day_type)
        log = log[log["space"] == space.id]
        sensible, latent = [0.0] * 24, [0.0] * 24
        for row in log.itertuples():
            gain = p["gains"].get(row.activity)
            if gain is None:
                continue
            factor = row.fraction * (p["child_factor"] if row.role == "child" else 1.0)
            sensible[row.hour] += gain["sensible"] * factor
            latent[row.hour] += gain["latent"] * factor
        return sensible, latent

    # ----- 一般家電 -----

    def _activities(self, spec) -> set[str]:
        return self.awake if spec == "awake" else set(spec)

    def appliance_hourly(self, appliance: Appliance, space: Space, weather: pd.DataFrame,
                         day_type: str, ac_on: list[float] | None = None) -> list[float]:
        """非冷氣、非可移轉家電的每小時耗電（W）。"""
        if not appliance.enabled:
            return [0.0] * 24
        rule = self.params["appliance_rules"].get(appliance.id)
        if rule is None:
            raise ValueError(f"家電 {appliance.id} 沒有設定運轉規則（load_params.json）")
        if rule["type"] == "always":
            return [appliance.power_w] * 24

        fractions = self.schedule.presence(day_type, space.id, self._activities(rule["activities"]))
        geo_daylight = self.params["geometry"]["by_template"][space.template]["daylight"]
        threshold = self.params["lighting"]["daylight_threshold_w_m2"]
        result = []
        for h, frac in enumerate(fractions):
            w = weather.iloc[h]
            if appliance.id == "lighting" and geo_daylight and w.solar_mj_m2 * MJ_PER_HOUR_TO_W >= threshold:
                frac = 0.0
            if "outdoor_below_c" in rule and w.temp_c >= rule["outdoor_below_c"]:
                frac = 0.0
            if "outdoor_rh_above" in rule and w.rh_pct <= rule["outdoor_rh_above"]:
                frac = 0.0
            if rule.get("not_cooling") and ac_on is not None:
                frac = max(0.0, frac - ac_on[h])
            result.append(appliance.power_w * frac * rule.get("duty", 1.0))
        return result

    # ----- 冷氣 -----

    def default_ac_on(self, space: Space, day_type: str, weather: pd.DataFrame) -> list[float]:
        """未最佳化的習慣：空間有人（含睡覺）就開冷氣；室外夠涼時改開窗通風。"""
        threshold = self.params["air_conditioner"]["natural_ventilation_below_c"]
        presence = self.schedule.presence(day_type, space.id)
        return [p if t >= threshold else 0.0 for p, t in zip(presence, weather.temp_c)]

    def space_hourly(self, space: Space, weather: pd.DataFrame, day_type: str,
                     setpoint_c: float | list[float] = 26.0,
                     ac_on: list[float] | None = None) -> pd.DataFrame:
        """某空間 24 小時的逐時結果：各家電耗電（W）、冷房負載組成、COP 等。

        weather：24 列，需有 temp_c、rh_pct、solar_mj_m2。
        setpoint_c：冷氣設定溫度，可為單一值或 24 小時列表。
        ac_on：每小時冷氣運轉比例（0–1）；None 時用「有人就開、室外涼爽時不開」。
        """
        setpoints = [setpoint_c] * 24 if isinstance(setpoint_c, (int, float)) else list(setpoint_c)
        ac_unit = space.get_appliance("air_conditioner")
        has_ac = ac_unit is not None and ac_unit.enabled
        if ac_on is None:
            ac_on = self.default_ac_on(space, day_type, weather) if has_ac else [0.0] * 24

        # 1. 一般家電（同時是室內發熱來源）
        appliance_w: dict[str, list[float]] = {}
        for a in space.appliances:
            if a.id == "air_conditioner" or a.id in self.params["shiftable_tasks"]:
                continue
            appliance_w[a.id] = self.appliance_hourly(a, space, weather, day_type, ac_on if has_ac else None)
        internal = [sum(v[h] for v in appliance_w.values()) for h in range(24)]

        # 2. 冷氣
        rows = []
        geo = SpaceGeometry.from_space(space, self.params)
        env, ac = self.params["envelope"], self.params["air_conditioner"]
        temps, rhs = list(weather.temp_c), list(weather.rh_pct)
        solar = [s * MJ_PER_HOUR_TO_W for s in weather.solar_mj_m2]
        sol_air = sol_air_temps(temps, solar, env)
        t_wall = effective_wall_temps(sol_air, env["wall_time_lag_h"], env["wall_decrement_factor"])
        ppl_s, ppl_l = self.people_gains(space, day_type)
        capacity_kj_k = self.params["thermal_mass"]["kj_per_m2_k"] * geo.floor_m2
        ua_closed = geo.conductance_w_k(self.params, self.params["infiltration"]["ach"])
        ua_open = geo.conductance_w_k(self.params, self.params["infiltration"]["natural_ventilation_ach"])
        ua_exhaust = self._exhaust_conductance(space, appliance_w)

        def free_running(h: int, t_start: float) -> tuple[float, float]:
            """冷氣沒開時的室溫，一階熱響應（1R1C 模型）：
                平衡溫度 T_eq = 室外與牆體等效溫度的平均 + 室內發熱 ÷ UA
                室溫以時間常數 τ = C ÷ UA 指數接近 T_eq（家具、牆面有熱容量，不會瞬間變溫）
            室外夠熱或適中時住戶會開窗（UA 大）；太冷時關窗；排油煙機、抽風機運轉時換氣再增加。
            回傳（這小時平均室溫, 小時結束時室溫）。"""
            ua = (ua_open if temps[h] >= ac["natural_ventilation_below_c"] else ua_closed) + ua_exhaust[h]
            base = (temps[h] + t_wall[h]) / 2
            if ua <= 0 or capacity_kj_k <= 0:   # 沒有面積的空間（公共設備）不計室溫
                return base, base
            t_eq = base + (ppl_s[h] + internal[h]) / ua
            tau_h = capacity_kj_k * 1000 / ua / 3600
            decay = math.exp(-1 / tau_h)
            t_end = t_eq + (t_start - t_eq) * decay
            t_avg = t_eq + (t_start - t_eq) * tau_h * (1 - decay)   # 指數曲線在一小時內的平均
            return t_avg, t_end

        # 跑兩輪：第一輪求出 23 時結束時的室溫，作為 0 時的起始狀態（每天週期性重複）
        t_room = (temps[23] + t_wall[23]) / 2
        for _ in range(2):
            rows = []
            for h in range(24):
                t_free, t_free_end = free_running(h, t_room)
                on = ac_on[h] if has_ac else 0.0
                pulldown = 0.0
                if on > 0 and t_room > setpoints[h]:
                    pulldown = capacity_kj_k * (t_room - setpoints[h]) / 3.6  # kJ → Wh（一小時平均 W）
                load = cooling_load(geo, self.params, temps[h], rhs[h], solar[h], t_wall[h], setpoints[h],
                                    ppl_s[h], ppl_l[h], internal[h], on, pulldown)
                if not has_ac or on <= 0:
                    load = CoolingLoad()  # 冷氣沒開就沒有冷房負載（只記錄耗電為 0）
                result = ac_power(load.total, ac_unit.power_w if has_ac else 0, temps[h], setpoints[h], ac, on)
                cooling = on > 0 and load.total > 0
                t_ac = self._cooled_room_temp(load, result, setpoints[h], t_room, ua_closed, capacity_kj_k, on) \
                    if cooling else t_free
                t_room = t_ac if on >= 0.5 and cooling else t_free_end
                rows.append({
                    "hour": h,
                    "setpoint_c": setpoints[h] if on > 0 else None,
                    "ac_on": on,
                    "room_temp_c": t_ac if cooling else t_free,   # 使用者在這小時感受到的室溫
                    **{f"load_{k}": v for k, v in load.as_dict().items()},
                    "load_total_w": load.total,
                    # 能力不足：只計「連穩態負載都處理不了」的部分；
                    # 蓄熱沒移除完的部分會轉到下一小時的 pull-down，不重複計算
                    "unmet_w": max(0.0, load.total - load.pulldown - result.delivered_w),
                    "cop": result.cop,
                    "part_load": result.part_load,
                    "air_conditioner": result.power_w,
                })

        df = pd.DataFrame(rows)
        for aid, values in appliance_w.items():
            df[aid] = values
        return df

    def _exhaust_conductance(self, space: Space, appliance_w: dict[str, list[float]]) -> list[float]:
        """排風家電（排油煙機、抽風機）運轉時帶走室內熱量的等效熱傳係數（W/K）。"""
        inf = self.params["infiltration"]
        result = [0.0] * 24
        for aid, hourly_w in appliance_w.items():
            rule = self.params["appliance_rules"].get(aid, {})
            appliance = space.get_appliance(aid)
            if "exhaust_m3_h" not in rule or appliance is None or appliance.power_w <= 0:
                continue
            for h, w in enumerate(hourly_w):
                running = w / (appliance.power_w * rule.get("duty", 1.0))  # 這小時運轉比例
                result[h] += inf["air_density"] * inf["cp_air"] * rule["exhaust_m3_h"] * running / 3600
        return result

    @staticmethod
    def _cooled_room_temp(load: CoolingLoad, result: "ACResult", t_set: float, t_prev: float,
                          ua: float, capacity_kj_k: float, on: float) -> float:
        """冷氣運轉這小時結束時的室溫。

        冷氣先處理「穩態負載」（外牆、日射、人體、家電、滲透），剩下的能力才用來移除蓄熱：
        - 穩態負載都處理不了：室溫高於設定值，溫升 = 未處理熱量 ÷ 熱傳係數 UA
        - 蓄熱只移除一部分：室溫從原本溫度往下降，降幅 = 移除熱量 ÷ 熱容量 C
        """
        steady = load.total - load.pulldown
        if result.delivered_w < steady:
            return t_set + (steady - result.delivered_w) / (ua * max(on, 1e-6))
        removed_wh = result.delivered_w - steady
        if load.pulldown > 0 and removed_wh < load.pulldown - 1e-9:
            return max(t_set, t_prev - removed_wh * 3.6 / capacity_kj_k)
        return t_set

    # ----- 可移轉負載 -----

    def _find_appliance(self, appliance_id: str) -> tuple[Space, Appliance] | None:
        for s, a in self.house.all_appliances():
            if a.id == appliance_id and a.enabled:
                return s, a
        return None

    def shiftable_tasks(self, weather: pd.DataFrame, day_type: str) -> list[dict]:
        """當天需要完成的可移轉工作（洗衣、烘衣、燒熱水）及其耗電量。"""
        cfg = self.params["shiftable_tasks"]
        mean_t, mean_rh = weather.temp_c.mean(), weather.rh_pct.mean()
        tasks = []
        for aid in ("washer", "dryer"):
            found = self._find_appliance(aid)
            if not found:
                continue
            space, a = found
            spec = cfg[aid]
            cond = spec.get("only_if")
            if cond and not (mean_rh > cond["daily_mean_rh_above"] or mean_t < cond["or_daily_mean_temp_below"]):
                continue
            for _ in range(spec["runs"][day_type]):
                tasks.append({"appliance": aid, "space": space.id, "name": a.name, "power_w": a.power_w,
                              "duration_h": spec["duration_h"],
                              "energy_kwh": a.power_w * spec["duration_h"] / 1000,
                              "default_start": spec["default_start"]})

        found = self._find_appliance("water_heater")
        if found:
            space, a = found
            spec = cfg["water_heater"]
            log = self.schedule.activity_log(day_type)
            showers = log[log["activity"] == "shower"]["member"].nunique()
            kg = showers * spec["liters_per_shower"]
            t_cold = mean_t  # 冷水溫度以日平均氣溫近似
            energy_kwh = kg * 4.186 * (spec["supply_temp_c"] - t_cold) / 3600 / spec["efficiency"]
            tasks.append({"appliance": "water_heater", "space": space.id, "name": a.name,
                          "power_w": a.power_w, "duration_h": energy_kwh * 1000 / a.power_w,
                          "energy_kwh": energy_kwh, "default_start": spec["default_start"]})
        return tasks


def task_hourly(task: dict, start_hour: float | None = None) -> list[float]:
    """把一項可移轉工作攤到 24 小時（W）；跨過午夜的部分繞回當天清晨（每天週期性重複）。"""
    start = task["default_start"] if start_hour is None else start_hour
    end = start + task["duration_h"]
    hourly = [0.0] * 24
    t = start
    while t < end - 1e-9:
        h = int(math.floor(t))
        step = min(h + 1, end) - t
        hourly[h % 24] += task["power_w"] * step
        t += step
    return hourly


if __name__ == "__main__":
    # 用法：python -m core.load_model → 夏季代表日主臥逐時冷房負載，以及冬夏各空間日耗電預覽
    from core.data_source import CwaCsvWeatherSource, load_representative_days
    from core.models import load_house
    from core.schedule import day_type_for
    from core.tariff import Tariff

    pd.set_option("display.unicode.east_asian_width", True)
    pd.set_option("display.width", 200)

    house = load_house()
    model = LoadModel(house, Schedule(house))
    days = load_representative_days()
    source, tariff = CwaCsvWeatherSource(), Tariff()

    # 1. 主臥逐時明細
    day = days["summer"]
    weather = source.get_hourly_weather(day)
    dt = day_type_for(day, tariff.is_off_peak_day)
    df = model.space_hourly(house.get_space("master_bedroom"), weather, dt, setpoint_c=26)
    show = pd.DataFrame({
        "室外°C": weather.temp_c, "冷氣開": df.ac_on.round(2),
        "外牆": df.load_wall, "窗傳熱": df.load_window_conduction, "日射": df.load_window_solar,
        "人體": df.load_people_sensible + df.load_people_latent, "家電": df.load_internal,
        "滲透": df.load_infiltration_sensible + df.load_infiltration_latent, "蓄熱": df.load_pulldown,
        "總負載W": df.load_total_w, "COP": df.cop, "冷氣耗電W": df.air_conditioner,
    }).round(1)
    print(f"=== 主臥 {day}（{dt}）設定 26°C，有人就開冷氣 ===")
    print(show.to_string())
    print(f"主臥冷氣全日耗電 {df.air_conditioner.sum() / 1000:.2f} kWh")

    # 2. 各空間全日耗電預覽（階段 5 會正式整合成推演引擎）
    for season, day in days.items():
        weather = source.get_hourly_weather(day)
        dt = day_type_for(day, tariff.is_off_peak_day)
        print(f"\n=== {season} {day}（{dt}）各空間全日耗電 kWh ===")
        total = 0.0
        for space in house.spaces:
            sdf = model.space_hourly(space, weather, dt, setpoint_c=26)
            cols = [a.id for a in space.appliances if a.id in sdf.columns]
            kwh = sdf[cols].sum() / 1000
            total += kwh.sum()
            detail = "、".join(f"{a.name} {kwh[a.id]:.2f}" for a in space.appliances if a.id in kwh and kwh[a.id] > 0)
            print(f"  {space.name:<6} {kwh.sum():6.2f}  （{detail}）")
        for t in model.shiftable_tasks(weather, dt):
            total += t["energy_kwh"]
            print(f"  {t['name']:<6} {t['energy_kwh']:6.2f}  （運轉 {t['duration_h']:.2f} h）")
        print(f"  全屋合計 {total:.2f} kWh")
