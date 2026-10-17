"""負載與耗電模型：給定空間、天氣、時間、設定溫度，算出每小時各家電耗電。

冷房負載（W）＝ 外牆傳熱 + 窗戶傳熱 + 窗戶日射（依窗戶方位與太陽位置） + 人體（顯熱＋潛熱）
             + 室內家電發熱 + 外氣滲透（顯熱＋潛熱）+ 蓄熱移除（pull-down）
冷氣耗電（W）＝ 冷房負載 ÷ COP；冷房能力與 COP 依性能曲線（室外乾球、室內濕球）與部分負載率變化，
             額定 COP 依能源效率等級。

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
from datetime import date
from functools import lru_cache
from pathlib import Path

import pandas as pd

from core.models import Appliance, House, LoadType, Space
from core.schedule import Schedule
from core.solar import hourly_vertical

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
MJ_PER_HOUR_TO_W = 1e6 / 3600        # 每小時日射量 MJ/m² → 平均日射強度 W/m²
NOT_AWAKE = {"sleep", "out"}


# ---------- 濕空氣性質 ----------

@lru_cache(maxsize=65536)
def saturation_pressure_kpa(t_c: float) -> float:
    """飽和水蒸氣壓（Magnus 公式，Alduchov & Eskridge 1996 係數）。"""
    return 0.61094 * math.exp(17.625 * t_c / (t_c + 243.04))


@lru_cache(maxsize=65536)
def humidity_ratio(t_c: float, rh_pct: float, p_kpa: float = 101.325) -> float:
    """絕對濕度（kg 水 / kg 乾空氣）。"""
    pw = rh_pct / 100 * saturation_pressure_kpa(t_c)
    return 0.622 * pw / (p_kpa - pw)


def relative_humidity(t_c: float, w: float, p_kpa: float = 101.325) -> float:
    """絕對濕度 → 相對濕度（%），上限 100。"""
    pw = w * p_kpa / (0.622 + w)
    return min(100.0, 100 * pw / saturation_pressure_kpa(t_c))


# ---------- 空間幾何 ----------

@dataclass
class SpaceGeometry:
    floor_m2: float
    volume_m3: float
    wall_m2: float      # 外牆（不含窗）
    window_m2: float
    daylight: bool

    @classmethod
    def from_space(cls, space: Space, params: dict) -> SpaceGeometry:
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
    latent_pulldown: float = 0.0   # 冷氣開機時移除室內（含家具、牆面）累積水氣的潛熱
    interzone: float = 0.0         # 隔間牆從相鄰房間傳入的熱（鄰房較涼時為負）

    @property
    def latent(self) -> float:
        # 冷氣只會除濕不會加濕，潛熱合計不小於 0
        return max(0.0, self.people_latent + self.infiltration_latent + self.latent_pulldown)

    @property
    def sensible(self) -> float:
        return (self.wall + self.window_conduction + self.window_solar + self.people_sensible
                + self.internal + self.infiltration_sensible + self.pulldown + self.interzone)

    @property
    def total(self) -> float:
        return max(0.0, self.sensible + self.latent)

    def as_dict(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in _LOAD_FIELDS}


_LOAD_FIELDS = [f.name for f in fields(CoolingLoad)]


def sol_air_temps(temps: list[float], solar_w_m2: list[float], env: dict,
                  vertical_w_m2: list[float] | None = None) -> list[float]:
    """等效日射溫度：外牆吸收日射後，相當於室外溫度升高多少。
    vertical_w_m2：牆面（垂直面）日射；未給時以水平日射 × 平均比例估算。"""
    vertical = vertical_w_m2 if vertical_w_m2 is not None else [s * env["vertical_irradiance_ratio"] for s in solar_w_m2]
    return [t + env["wall_absorptance"] * v / env["h_out"] for t, v in zip(temps, vertical)]


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
    vertical_w_m2: float | None = None,
    latent_pulldown_w: float = 0.0,
    interzone_w: float = 0.0,
) -> CoolingLoad:
    """計算某空間某小時的冷房負載。on_fraction 為冷氣這小時運轉的時間比例；
    人體與家電發熱已是一小時平均值，不再乘比例。
    vertical_w_m2：窗戶所在牆面的垂直面日射（依方位，core.solar）；未給時以水平日射 × 平均比例估算。"""
    env, inf = params["envelope"], params["infiltration"]
    vertical = vertical_w_m2 if vertical_w_m2 is not None else solar_w_m2 * env["vertical_irradiance_ratio"]
    airflow_m3_s = geo.volume_m3 * inf["ach"] / 3600
    w_out = humidity_ratio(t_out, rh_out)
    w_in = humidity_ratio(t_set, inf["indoor_rh_pct"])
    f = on_fraction
    return CoolingLoad(
        wall=f * env["u_wall"] * geo.wall_m2 * (t_wall_eff - t_set),
        window_conduction=f * env["u_window"] * geo.window_m2 * (t_out - t_set),
        window_solar=f * geo.window_m2 * vertical * env["shgc"] * env["interior_shading"],
        people_sensible=people_sensible_w,
        people_latent=people_latent_w,
        internal=internal_w,
        infiltration_sensible=f * inf["air_density"] * inf["cp_air"] * airflow_m3_s * (t_out - t_set),
        infiltration_latent=f * inf["air_density"] * airflow_m3_s * inf["h_fg"] * (w_out - w_in),
        pulldown=pulldown_w,
        latent_pulldown=latent_pulldown_w,
        interzone=interzone_w,
    )


# ---------- 冷氣效率 ----------

@lru_cache(maxsize=65536)
def wet_bulb_c(t_c: float, rh_pct: float) -> float:
    """濕球溫度（Stull 2011 經驗式，適用 RH 5–99%、−20–50°C，誤差約 ±0.3°C）。"""
    rh = min(max(rh_pct, 5.0), 99.0)
    return (t_c * math.atan(0.151977 * math.sqrt(rh + 8.313659)) + math.atan(t_c + rh) - math.atan(rh - 1.676331)
            + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh) - 4.686035)


def _biquadratic(c: list[float], x: float, y: float) -> float:
    return c[0] + c[1] * x + c[2] * x * x + c[3] * y + c[4] * y * y + c[5] * x * y


def performance_factors(t_out: float, t_wb_in: float, ac: dict) -> tuple[float, float]:
    """冷氣性能曲線：回傳（冷房能力倍率, COP 倍率），額定條件（室內濕球 19°C、室外 35°C）時皆為 1。
    能力倍率 = CapFT(x, y) ÷ CapFT(額定)；COP 倍率 = EIRFT(額定) ÷ EIRFT(x, y)。"""
    return _performance(tuple(ac["cap_ft"]), tuple(ac["eir_ft"]), ac["rated_indoor_wb_c"], ac["rated_outdoor_c"],
                        t_out, t_wb_in)


@lru_cache(maxsize=65536)
def _performance(cap_ft: tuple, eir_ft: tuple, x0: float, y0: float, t_out: float, t_wb_in: float):
    cap = _biquadratic(cap_ft, t_wb_in, t_out) / _biquadratic(cap_ft, x0, y0)
    cop = _biquadratic(eir_ft, x0, y0) / _biquadratic(eir_ft, t_wb_in, t_out)
    return cap, cop


def indoor_wet_bulb(t_set: float, rh_in: float | None, ac: dict) -> float:
    """室內回風濕球溫度；rh_in 未給時用額定條件的濕度（使設定溫度等於額定室內溫度時正好是額定點）。"""
    if rh_in is None:
        return ac["rated_indoor_wb_c"] + (t_set - ac["rated_indoor_c"]) * 0.75   # 等濕度下濕球隨乾球的變化率約 0.75
    return wet_bulb_c(t_set, rh_in)


def ac_cop(t_out: float, t_set: float, part_load: float, ac: dict,
           rated_cop: float | None = None, rh_in: float | None = None) -> float:
    """冷氣 COP：額定值 × 性能曲線（室外溫度、室內濕球）× 部分負載修正。"""
    rated_cop = ac["rated_cop"] if rated_cop is None else rated_cop
    _, cop_factor = performance_factors(t_out, indoor_wet_bulb(t_set, rh_in, ac), ac)
    plr_min = ac["min_part_load"]
    if part_load >= plr_min:
        # 變頻機降頻運轉時效率較高
        plf = 1 + ac["part_load_gain"] * (1 - part_load)
    else:
        # 低於最低轉速，壓縮機開開停停，效率下降
        plf_min = 1 + ac["part_load_gain"] * (1 - plr_min)
        plf = plf_min * (1 - ac["cycling_degradation"] * (1 - part_load / plr_min))
    return max(ac["min_cop"], rated_cop * cop_factor * plf)


def ac_rating(appliance: Appliance, ac: dict) -> tuple[float, float]:
    """一台冷氣的（額定耗電 W, 額定 COP）。有設定能力（kW）時，額定耗電 = 能力 ÷ 該等級 COP。"""
    rated_cop = ac["grade_cop"][str(appliance.grade)] if appliance.grade else ac["rated_cop"]
    if appliance.capacity_kw:
        return appliance.capacity_kw * 1000 / rated_cop, rated_cop
    return appliance.power_w, rated_cop


@dataclass
class ACResult:
    power_w: float       # 耗電（W，一小時平均 = 該小時 Wh）
    delivered_w: float   # 實際移除的熱量
    unmet_w: float       # 能力不足、未移除的負載
    cop: float
    part_load: float


def ac_power(load_w: float, rated_power_w: float, t_out: float, t_set: float,
             ac: dict, on_fraction: float = 1.0, rated_cop: float | None = None,
             rh_in: float | None = None) -> ACResult:
    """冷氣耗電 = 冷房負載 ÷ COP；冷房能力 = 額定耗電 × 額定 COP × 能力倍率（室外越熱能力越低）。"""
    if load_w <= 0 or on_fraction <= 0:
        return ACResult(0.0, 0.0, 0.0, 0.0, 0.0)
    rated_cop = ac["rated_cop"] if rated_cop is None else rated_cop
    cap_factor, _ = performance_factors(t_out, indoor_wet_bulb(t_set, rh_in, ac), ac)
    capacity = rated_power_w * rated_cop * cap_factor * on_fraction
    delivered = min(load_w, capacity)
    part_load = delivered / capacity
    cop = ac_cop(t_out, t_set, part_load, ac, rated_cop, rh_in)
    return ACResult(delivered / cop, delivered, load_w - delivered, cop, part_load)


# ---------- 房間之間的熱交換（A2） ----------

def default_adjacency(house: House, params: dict) -> dict[tuple[str, str], float]:
    """沒有平面圖時的相鄰關係：每個房間都與客廳（公共區域）共用一面隔間牆，
    牆長取地板面積的平方根（正方形房間的一邊）。台灣公寓的臥室、浴室、廚房多半開向客廳或走道。
    回傳 {(房間 a, 房間 b): 共用牆長 m}。"""
    rooms = [s for s in house.spaces if s.area_ping > 0]
    hubs = [s for s in rooms if s.template == "living_room"]
    if not hubs:
        return {}
    hub = max(hubs, key=lambda s: s.area_ping)
    ping = params["geometry"]["ping_to_m2"]
    return {tuple(sorted((s.id, hub.id))): round(math.sqrt(s.area_ping * ping), 2) for s in rooms if s is not hub}


# ---------- 整合：空間逐時耗電 ----------

def load_params(path: Path = CONFIG_DIR / "load_params.json") -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class LoadModel:
    """結合房屋、作息、參數，計算各空間 24 小時耗電。"""

    def __init__(self, house: House, schedule: Schedule, params: dict | None = None,
                 adjacency: dict[tuple[str, str], float] | None = None):
        """adjacency：{(房間 a, 房間 b): 共用隔間牆長度 m}，介面由平面圖算出；None 時用 default_adjacency。"""
        self.house = house
        self.schedule = schedule
        self.params = params or load_params()
        self.adjacency = default_adjacency(house, self.params) if adjacency is None else dict(adjacency)
        # 隔間熱傳係數 UA（W/K）= U × 牆長 × 樓高
        iz, height = self.params["interzone"], self.params["geometry"]["ceiling_height_m"]
        ids = {s.id for s in house.spaces if s.area_ping > 0}
        self.interzone_ua: dict[str, dict[str, float]] = {}
        for (a, b), length in self.adjacency.items():
            if a in ids and b in ids and length > 0:
                ua = iz["u_partition"] * length * height
                self.interzone_ua.setdefault(a, {})[b] = ua
                self.interzone_ua.setdefault(b, {})[a] = ua
        self.awake = set(schedule.activities) - NOT_AWAKE
        self._people_cache: dict[tuple, tuple[list[float], list[float]]] = {}
        self._prep_cache: dict[tuple, tuple] = {}
        # 有設定能力（kW）與能效等級的冷氣：額定耗電由能力 ÷ COP 推得，讓介面與排程顯示一致
        for _, a in house.all_appliances():
            if a.id == "air_conditioner" and a.capacity_kw:
                a.power_w = ac_rating(a, self.params["air_conditioner"])[0]

    def __getstate__(self) -> dict:
        """傳給其他程序平行計算時不帶快取（快取很大，到了另一邊會重新建立）。"""
        state = self.__dict__.copy()
        state["_prep_cache"] = {}
        return state

    # ----- 方位日射 -----

    def with_solar_geometry(self, weather: pd.DataFrame, day: date) -> pd.DataFrame:
        """加上各方位垂直面日射欄位 vert_<方位>（W/m²），vert_avg 為四面平均（未指定方位的空間）。
        推演引擎與最佳化都先呼叫這裡，兩邊用同一份日射。"""
        site = self.params["site"]
        ghi = [x * MJ_PER_HOUR_TO_W for x in weather.solar_mj_m2]
        out = weather.copy()
        for o in [None, *sorted({s.orientation for s in self.house.spaces if s.orientation})]:
            out[f"vert_{o or 'avg'}"] = hourly_vertical(day, ghi, site["latitude"], site["longitude"], o,
                                                        site["timezone_h"], site["ground_albedo"])
        return out

    # ----- 人體發熱 -----

    def people_gains(self, space: Space, day_type: str) -> tuple[list[float], list[float]]:
        """每小時人體顯熱、潛熱（W）。只和作息有關，算過就快取（最佳化時會呼叫數百次）。"""
        key = (space.id, day_type)
        if key not in self._people_cache:
            self._people_cache[key] = self._compute_people_gains(space, day_type)
        sensible, latent = self._people_cache[key]
        return list(sensible), list(latent)

    def _compute_people_gains(self, space: Space, day_type: str) -> tuple[list[float], list[float]]:
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

    def activity_moisture(self, space: Space, day_type: str) -> list[float]:
        """每小時活動額外產生的水氣（kg/h）：洗澡的熱水蒸氣、煮飯的蒸氣（人體本身的水氣在潛熱裡）。"""
        key = ("moisture", space.id, day_type)
        if key not in self._people_cache:
            rates = self.params["humidity"]["activity_moisture_kg_h"]
            log = self.schedule.activity_log(day_type)
            log = log[(log["space"] == space.id) & (log["activity"].isin(list(rates)))]
            hourly = [0.0] * 24
            for row in log.itertuples():
                hourly[row.hour] += rates[row.activity] * row.fraction
            self._people_cache[key] = (hourly, [])
        return list(self._people_cache[key][0])

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
        temps, rhs, solar = list(weather.temp_c), list(weather.rh_pct), list(weather.solar_mj_m2)
        result = []
        for h, frac in enumerate(fractions):
            if appliance.id == "lighting" and geo_daylight and solar[h] * MJ_PER_HOUR_TO_W >= threshold:
                frac = 0.0
            if "outdoor_below_c" in rule and temps[h] >= rule["outdoor_below_c"]:
                frac = 0.0
            if "outdoor_rh_above" in rule and rhs[h] <= rule["outdoor_rh_above"]:
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
                     ac_on: list[float] | None = None,
                     appliance_factors: dict[str, list[float]] | None = None,
                     appliance_forced: dict[str, list[float | None]] | None = None,
                     neighbor_temps: dict[str, list[float]] | None = None,
                     as_frame: bool = True) -> pd.DataFrame | dict[str, list]:
        """某空間 24 小時的逐時結果：各家電耗電（W）、冷房負載組成、COP 等。

        weather：24 列，需有 temp_c、rh_pct、solar_mj_m2。
        setpoint_c：冷氣設定溫度，可為單一值或 24 小時列表。
        ac_on：每小時冷氣運轉比例（0–1）；None 時用「有人就開、室外涼爽時不開」。
        appliance_factors：家電 id → 24 小時調整係數（0–1），例如照明調光 0.7、除濕機關閉 0。
                           必要負載不可調整。
        neighbor_temps：相鄰房間 24 小時室溫（推演引擎反覆計算時傳入）；None 代表不計隔間熱交換。
        as_frame：False 時回傳 {欄名: 24 小時列表}，省下建立 DataFrame 的時間（最佳化反覆呼叫用）。
        appliance_forced：家電 id → 24 小時手動覆寫（遙控器）；None 代表依自動規則，
                          數值為運轉比例（0 = 關、1 = 全開、0.5 = 亮度 50%），不論有沒有人都照做。
        """
        setpoints = [setpoint_c] * 24 if isinstance(setpoint_c, (int, float)) else list(setpoint_c)
        ac_unit = space.get_appliance("air_conditioner")
        has_ac = ac_unit is not None and ac_unit.enabled
        if ac_on is None:
            ac_on = self.default_ac_on(space, day_type, weather) if has_ac else [0.0] * 24
        prep = self._prepare(space, weather, day_type, ac_on, has_ac, appliance_factors, appliance_forced)
        appliance_w = {k: list(v) for k, v in prep["appliance_w"].items()}   # 除濕機逐時改寫，複製一份
        dehum = prep["dehum"]
        base_internal = prep["base_internal"]
        internal = list(base_internal)
        geo, temps, rhs, solar, vertical, t_wall = (prep[k] for k in
                                                    ("geo", "temps", "rhs", "solar", "vertical", "t_wall"))
        ppl_s, ppl_l, ua_exhaust, w_outs, gen_kg_h, exhaust_m3_h = (
            prep[k] for k in ("ppl_s", "ppl_l", "ua_exhaust", "w_outs", "gen_kg_h", "exhaust_m3_h"))
        capacity_kj_k, ua_closed, ua_open, moist_cap_kg = (
            prep[k] for k in ("capacity_kj_k", "ua_closed", "ua_open", "moist_cap_kg"))
        ac = self.params["air_conditioner"]
        inf = self.params["infiltration"]
        rated_w, rated_cop = ac_rating(ac_unit, ac) if has_ac else (0.0, ac["rated_cop"])
        # 隔間熱交換：Σ UA_ij（W/K）與 Σ UA_ij × T_j(h)（相鄰房間室溫為已知，來自上一輪推演）
        ua_iz, q_iz = 0.0, [0.0] * 24
        if neighbor_temps:
            for nid, ua in self.interzone_ua.get(space.id, {}).items():
                if nid in neighbor_temps:
                    ua_iz += ua
                    q_iz = [q + ua * t for q, t in zip(q_iz, neighbor_temps[nid])]
        rh_ac = inf["indoor_rh_pct"]
        vent_open = inf["air_density"] * geo.volume_m3 * inf["natural_ventilation_ach"]
        vent_closed = inf["air_density"] * geo.volume_m3 * inf["ach"]
        nv_below = ac["natural_ventilation_below_c"]

        def vent_kg_h(h: int) -> float:
            """這小時的換氣量（kg 乾空氣/h）：開窗或關窗的換氣 + 排風家電。"""
            return (vent_open if temps[h] >= nv_below else vent_closed) + inf["air_density"] * exhaust_m3_h[h]

        def free_moisture(h: int, w_start: float, removal_kg_h: float = 0.0) -> float:
            """冷氣沒開時，這小時結束時的室內絕對濕度（一階反應，同室溫的 1R1C）。"""
            m = vent_kg_h(h)
            if m <= 0 or moist_cap_kg <= 0:
                return w_outs[h]
            w_eq = w_outs[h] + (gen_kg_h[h] - removal_kg_h) / m
            return w_eq + (w_start - w_eq) * math.exp(-m / moist_cap_kg)

        def dehumidify(h: int, w_start: float, t_room_c: float) -> tuple[float, float]:
            """除濕機這小時的（運轉比例, 除濕量 kg/h）：室內濕度高於目標才運轉，只運轉到剛好達標所需的時間。"""
            if dehum is None:
                return 0.0, 0.0
            cap_kg_h = dehum["rule"]["removal_l_per_day"] / 24
            if dehum["forced"][h] is not None:
                f = dehum["forced"][h]
                return f, cap_kg_h * f
            if dehum["avail"][h] <= 0 or moist_cap_kg <= 0:
                return 0.0, 0.0
            w_target = humidity_ratio(t_room_c, dehum["rule"]["target_rh_pct"])
            if free_moisture(h, w_start) <= w_target:
                return 0.0, 0.0
            m = vent_kg_h(h)
            decay = math.exp(-m / moist_cap_kg)
            w_eq_needed = (w_target - w_start * decay) / (1 - decay)        # 小時結束剛好達標所需的平衡濕度
            removal = gen_kg_h[h] - m * (w_eq_needed - w_outs[h])
            f = min(dehum["avail"][h], max(0.0, removal) / cap_kg_h)
            return f, cap_kg_h * f

        def free_running(h: int, t_start: float) -> tuple[float, float]:
            """冷氣沒開時的室溫，一階熱響應（1R1C 模型）：
                平衡溫度 T_eq =（UA_外 × 室外與牆體等效溫度 + Σ UA_鄰 × 鄰房室溫 + 室內發熱）÷ UA_總
                室溫以時間常數 τ = C ÷ UA 指數接近 T_eq（家具、牆面有熱容量，不會瞬間變溫）
            室外夠熱或適中時住戶會開窗（UA 大）；太冷時關窗；排油煙機、抽風機運轉時換氣再增加。
            回傳（這小時平均室溫, 小時結束時室溫）。"""
            ua_out = (ua_open if temps[h] >= nv_below else ua_closed) + ua_exhaust[h]
            base = (temps[h] + t_wall[h]) / 2
            if ua_out <= 0 or capacity_kj_k <= 0:   # 沒有面積的空間（公共設備）不計室溫
                return base, base
            ua = ua_out + ua_iz                    # 相鄰房間也是一個「外界」：隔間牆的熱傳
            t_eq = (ua_out * base + q_iz[h] + ppl_s[h] + internal[h]) / ua
            tau_h = capacity_kj_k * 1000 / ua / 3600
            decay = math.exp(-1 / tau_h)
            t_end = t_eq + (t_start - t_eq) * decay
            t_avg = t_eq + (t_start - t_eq) * tau_h * (1 - decay)   # 指數曲線在一小時內的平均
            return t_avg, t_end

        # 跑兩輪：第一輪求出 23 時結束時的室溫，作為 0 時的起始狀態（每天週期性重複）；
        # 只有第二輪記錄逐時明細（第一輪只用來決定起始狀態）
        t_room = (temps[23] + t_wall[23]) / 2
        w_room = w_outs[23]
        no_load = CoolingLoad()
        for record in (False, True):
            rows = []
            for h in range(24):
                on = ac_on[h] if has_ac else 0.0
                # 除濕機（只在冷氣沒開時）：除下的水氣潛熱與耗電都變成室內顯熱
                f_dehum, removal = dehumidify(h, w_room, t_room) if on <= 0 else (0.0, 0.0)
                if dehum is not None:
                    appliance_w[dehum["id"]][h] = dehum["power_w"] * f_dehum
                    internal[h] = base_internal[h] + dehum["power_w"] * f_dehum + removal * inf["h_fg"] / 3600
                t_free, t_free_end = free_running(h, t_room)
                w_target = humidity_ratio(setpoints[h], rh_ac)
                if on > 0:
                    pulldown = capacity_kj_k * (t_room - setpoints[h]) / 3.6 if t_room > setpoints[h] else 0.0
                    args = (geo, self.params, temps[h], rhs[h], solar[h], t_wall[h], setpoints[h],
                            ppl_s[h], ppl_l[h], internal[h], on, pulldown,
                            vertical[h] if vertical is not None else None)
                    iz_w = on * (q_iz[h] - ua_iz * setpoints[h])     # 鄰房比設定溫度熱，熱量經隔間牆傳入
                    load = cooling_load(*args, interzone_w=iz_w)
                    if w_room > w_target:
                        # 累積的水氣（含家具牆面吸附）：冷氣先處理顯熱與穩態負載，剩下的能力才用來除濕，
                        # 沒除完的留到下一小時（實際上要數小時才會降到目標濕度）
                        cap_factor, _ = performance_factors(temps[h], indoor_wet_bulb(setpoints[h], rh_ac, ac), ac)
                        spare = max(0.0, rated_w * rated_cop * cap_factor * on - load.total)
                        latent_pulldown = min(moist_cap_kg * (w_room - w_target) * inf["h_fg"] / 3600, spare)
                        load = cooling_load(*args, latent_pulldown, interzone_w=iz_w)
                    if not has_ac:
                        load = no_load
                else:
                    load = no_load    # 冷氣沒開就沒有冷房負載（只記錄耗電為 0）
                total = load.total
                result = ac_power(total, rated_w, temps[h], setpoints[h], ac, on, rated_cop, rh_ac)
                cooling = on > 0 and total > 0
                t_ac = self._cooled_room_temp(load, result, setpoints[h], t_room, ua_closed + ua_iz, capacity_kj_k, on) \
                    if cooling else t_free
                t_room = t_ac if on >= 0.5 and cooling else t_free_end
                w_start = w_room
                if on >= 0.5 and cooling:   # 冷氣維持目標濕度，另外除掉 latent_pulldown 對應的累積水氣
                    removed_kg = load.latent_pulldown * 3600 / inf["h_fg"]
                    w_room = max(w_target, w_room - removed_kg / moist_cap_kg) if w_room > w_target else w_room
                else:
                    w_room = free_moisture(h, w_room, removal)
                if not record:
                    continue
                t_felt = t_ac if cooling else t_free          # 使用者在這小時感受到的室溫
                rows.append({
                    "hour": h,
                    "setpoint_c": setpoints[h] if on > 0 else None,
                    "ac_on": on,
                    "room_temp_c": t_felt,
                    # 這小時經隔間牆從鄰房傳入的熱（W，以實際室溫計；推演引擎用來檢查能量守恆）
                    "interzone_w": q_iz[h] - ua_iz * t_felt,
                    "room_rh_pct": relative_humidity(t_felt, (w_start + w_room) / 2),
                    **{f"load_{k}": v for k, v in load.as_dict().items()},
                    "load_total_w": total,
                    # 能力不足：只計「連穩態負載都處理不了」的部分；
                    # 蓄熱沒移除完的部分會轉到下一小時的 pull-down，不重複計算
                    "unmet_w": max(0.0, total - load.pulldown - result.delivered_w),
                    "cop": result.cop,
                    "part_load": result.part_load,
                    "air_conditioner": result.power_w,
                })

        # 一次建立整張表（逐欄加入 DataFrame 很慢，最佳化時每天會呼叫數百次）
        columns = {key: [row[key] for row in rows] for key in rows[0]}
        columns.update(appliance_w)
        return pd.DataFrame(columns) if as_frame else columns

    def _prepare(self, space: Space, weather: pd.DataFrame, day_type: str, ac_on: list[float], has_ac: bool,
                 appliance_factors: dict | None, appliance_forced: dict | None) -> dict:
        """逐時迴圈前、與設定溫度無關的部分：一般家電耗電、日射與牆體溫度、人體發熱、換氣、水氣來源。
        最佳化搜尋設定溫度時，同一空間同一天會呼叫數百次，這部分完全相同，快取起來只算一次（D3）。"""
        key = (space.id, id(weather), day_type, tuple(ac_on),
               repr(sorted((appliance_factors or {}).items())), repr(sorted((appliance_forced or {}).items())))
        hit = self._prep_cache.get(key)
        if hit is not None and hit[0] is weather:
            return hit[1]
        appliance_factors = appliance_factors or {}

        # 1. 一般家電（同時是室內發熱來源）
        appliance_w: dict[str, list[float]] = {}
        for a in space.appliances:
            if a.id == "air_conditioner" or a.id in self.params["shiftable_tasks"]:
                continue
            hourly = self.appliance_hourly(a, space, weather, day_type, ac_on if has_ac else None)
            if a.id in appliance_factors:
                if a.load_type is LoadType.ESSENTIAL:
                    raise ValueError(f"{a.name} 是必要負載，任何模式都不可調整")
                hourly = [w * f for w, f in zip(hourly, appliance_factors[a.id])]
            forced = (appliance_forced or {}).get(a.id)
            if forced is not None:
                hourly = [a.power_w * f if f is not None else w for w, f in zip(hourly, forced)]
            appliance_w[a.id] = hourly
        # 除濕機：上面算的是「有人、冷氣沒開時可以運轉」的上限，實際運轉依室內濕度在逐時迴圈決定；
        # 遙控器手動開關時照做
        dehum = None
        for aid, hourly in appliance_w.items():
            rule = self.params["appliance_rules"].get(aid, {})
            if rule.get("humidity_control"):
                unit = space.get_appliance(aid)
                forced = (appliance_forced or {}).get(aid) or [None] * 24
                dehum = {"id": aid, "power_w": unit.power_w, "rule": rule, "forced": forced,
                         "avail": [w / unit.power_w if unit.power_w else 0.0 for w in hourly]}
                appliance_w[aid] = [0.0] * 24
        base_internal = [sum(v[h] for v in appliance_w.values()) for h in range(24)]

        # 2. 外殼、日射、人體、換氣、水氣
        geo = SpaceGeometry.from_space(space, self.params)
        env, inf, hum = self.params["envelope"], self.params["infiltration"], self.params["humidity"]
        temps, rhs = list(weather.temp_c), list(weather.rh_pct)
        solar = [x * MJ_PER_HOUR_TO_W for x in weather.solar_mj_m2]
        vert_col = f"vert_{space.orientation or 'avg'}"
        vertical = list(weather[vert_col]) if vert_col in weather.columns else None   # 沒有日期時用平均比例
        sol_air = sol_air_temps(temps, solar, env, vertical)
        t_wall = effective_wall_temps(sol_air, env["wall_time_lag_h"], env["wall_decrement_factor"])
        ppl_s, ppl_l = self.people_gains(space, day_type)
        ua_exhaust = self._exhaust_conductance(space, appliance_w)
        prep = {
            "appliance_w": appliance_w, "dehum": dehum, "base_internal": base_internal, "geo": geo,
            "temps": temps, "rhs": rhs, "solar": solar, "vertical": vertical, "t_wall": t_wall,
            "ppl_s": ppl_s, "ppl_l": ppl_l, "ua_exhaust": ua_exhaust,
            "capacity_kj_k": self.params["thermal_mass"]["kj_per_m2_k"] * geo.floor_m2,
            "ua_closed": geo.conductance_w_k(self.params, inf["ach"]),
            "ua_open": geo.conductance_w_k(self.params, inf["natural_ventilation_ach"]),
            # 水氣平衡：室內空氣 + 家具牆面吸放濕的緩衝，相當於 buffer 倍的空氣量（kg 乾空氣）
            "moist_cap_kg": inf["air_density"] * geo.volume_m3 * hum["moisture_buffer_factor"],
            "w_outs": [humidity_ratio(t, rh) for t, rh in zip(temps, rhs)],
            # 水氣來源：人體（潛熱 ÷ 汽化熱）＋ 活動（洗澡、煮飯的蒸氣）
            "gen_kg_h": [lat_w / inf["h_fg"] * 3600 + extra
                         for lat_w, extra in zip(ppl_l, self.activity_moisture(space, day_type))],
            "exhaust_m3_h": [u / (inf["air_density"] * inf["cp_air"]) * 3600 for u in ua_exhaust],
        }
        if len(self._prep_cache) > 512:
            self._prep_cache.clear()
        self._prep_cache[key] = (weather, prep)      # 保留 weather 參考，id 不會被重複使用
        return prep

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
    def _cooled_room_temp(load: CoolingLoad, result: ACResult, t_set: float, t_prev: float,
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
                          "energy_kwh": energy_kwh, "base_energy_kwh": energy_kwh,
                          "default_start": spec["default_start"],
                          # 熱水要在第一個人洗澡前燒好；燒好到使用之間有散熱損失
                          "ready_by": self.schedule.first_activity_time(day_type, "shower"),
                          "standby_loss_per_h": spec["standby_loss_per_h"]})
        return tasks


def place_task(task: dict, start: float | None = None) -> dict:
    """決定可移轉工作的開始時間，回傳加上 start（與熱水器的等待時間、實際耗電）的新 dict。

    電熱水器：燒好之後到第一個人洗澡前，儲水桶持續散熱，
    需多燒 base × standby_loss_per_h × 等待時數。若來不及在洗澡前燒好，feasible = False。
    """
    start = task["default_start"] if start is None else start
    placed = {**task, "start": start % 24, "feasible": True}
    if "ready_by" not in task:
        return placed
    gap = (task["ready_by"] - start) % 24          # 從開始燒到需要用熱水的時間
    energy = task["base_energy_kwh"]
    for _ in range(5):                               # 耗電量和等待時間互相影響，迭代幾次即收斂
        duration = energy * 1000 / task["power_w"]
        wait = max(0.0, gap - duration)
        energy = task["base_energy_kwh"] * (1 + task["standby_loss_per_h"] * wait)
    duration = energy * 1000 / task["power_w"]
    placed.update(energy_kwh=energy, duration_h=duration, wait_h=max(0.0, gap - duration),
                  feasible=duration <= gap + 1e-9)
    return placed


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
