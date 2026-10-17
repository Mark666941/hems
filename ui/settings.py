"""介面設定 ↔ 核心模組：把使用者在介面上的修改轉成設定字典，再組裝整個系統。

設計原則：
- 介面只修改「設定字典」，不寫回 config/ 的 JSON 檔，預設值永遠保留。
- 所有設定序列化成一個 JSON 字串，當作快取的 key：設定沒變就不重新計算。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pandas as pd

from core.data_source import CwaCsvWeatherSource, OpenMeteoWeatherSource, WeatherSource
from core.load_model import LoadModel, load_params
from core.metrics import Analyzer
from core.models import House, house_from_config, load_appliance_catalog, load_space_templates
from core.optimizer import Optimizer
from core.schedule import Schedule
from core.simulator import ControlPlan, Simulator, load_baseline_plan
from core.solar import ORIENTATION_NAMES

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _read(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def default_settings() -> dict:
    """預設設定（全部來自 config/），介面以這份為起點讓使用者修改。"""
    baseline = load_baseline_plan()
    return {
        "house": _read("default_house.json"),
        "appliance_edits": {},            # "空間id/家電id" → {"power_w", "enabled"，冷氣另有 "capacity_kw", "grade"}
        "schedule": _read("schedule.json"),
        "modes": _read("modes.json"),
        "baseline_setpoint_c": baseline.default_setpoint,
        "location": dict(DEFAULT_LOCATION),
    }


# ---------- 地區與天氣（第 6b 批） ----------

DEFAULT_LOCATION = {"source": "cwa", "county": "臺中市", "town": "北區"}   # 中央氣象署臺中測站位於臺中市北區
WEATHER_SOURCES = {"cwa": "中央氣象署臺中測站（實測，離線可用）",
                   "openmeteo": "Open-Meteo（選擇地區、含未來 7 天預報，需要網路）"}
_TOWNS: dict | None = None


def load_towns() -> dict[str, dict[str, list[float]]]:
    """縣市 → 鄉鎮市區 → [緯度, 經度]（內政部鄉鎮市區界線算出的中心點，config/towns.json）。"""
    global _TOWNS
    if _TOWNS is None:
        _TOWNS = _read("towns.json")["towns"]
    return _TOWNS


def location_of(settings: dict) -> dict:
    """設定中的地區（舊設定檔沒有時用預設：臺中測站）。"""
    return {**DEFAULT_LOCATION, **settings.get("location", {})}


def location_key(settings: dict) -> str:
    return json.dumps(location_of(settings), ensure_ascii=False, sort_keys=True)


def location_coords(settings: dict) -> tuple[float, float]:
    loc = location_of(settings)
    lat, lon = load_towns()[loc["county"]][loc["town"]]
    return lat, lon


def location_label(settings: dict) -> str:
    """畫面上的地區標示，例如「臺中市｜中央氣象署臺中測站」「臺北市大安區｜Open-Meteo」。"""
    loc = location_of(settings)
    if loc["source"] == "cwa":
        return "臺中市｜中央氣象署臺中測站"
    return f"{loc['county']}{loc['town']}｜Open-Meteo"


def weather_source(settings: dict) -> WeatherSource:
    loc = location_of(settings)
    if loc["source"] == "openmeteo":
        return OpenMeteoWeatherSource(*location_coords(settings))
    return CwaCsvWeatherSource()


def to_key(settings: dict) -> str:
    """設定 → 穩定的 JSON 字串（快取 key）。"""
    return json.dumps(settings, ensure_ascii=False, sort_keys=True)


def adjacency(settings: dict) -> dict[tuple[str, str], float]:
    """由平面圖算出房間相鄰關係與共用牆長（隔間熱交換用）。"""
    from ui.floorplan import adjacency_from_layout  # floorplan 會匯入 streamlit 元件，需要時才載入
    return adjacency_from_layout(settings, load_space_templates())


def sim_key(settings: dict) -> str:
    """模擬用的快取 key：排除平面圖上的圖示與房間座標（只影響畫面），改放「相鄰關係」——
    拖曳家電圖示、或移動房間但鄰居不變時不必重新最佳化；房間換了鄰居（隔間熱交換改變）才重算。"""
    key = {k: v for k, v in settings.items() if k != "layout"}
    key["_adjacency"] = sorted([*pair, length] for pair, length in adjacency(settings).items())
    return to_key(key)


def build_house(settings: dict) -> House:
    house = house_from_config(settings["house"], load_space_templates(), load_appliance_catalog())
    for key, edit in settings["appliance_edits"].items():
        space_id, appliance_id = key.split("/")
        try:
            appliance = house.get_space(space_id).get_appliance(appliance_id)
        except KeyError:
            continue   # 空間已被刪除，忽略舊的修改
        if appliance is not None:
            appliance.power_w = float(edit.get("power_w", appliance.power_w))
            appliance.enabled = bool(edit.get("enabled", appliance.enabled))
            if appliance.id == "air_conditioner":       # 冷氣能力（kW）與能效等級；None 代表沿用功率推算
                appliance.capacity_kw = edit.get("capacity_kw", appliance.capacity_kw)
                appliance.grade = edit.get("grade", appliance.grade)
    return house


def build_analyzer(settings: dict, params: dict | None = None) -> Analyzer:
    """依設定組裝：房屋 → 作息 → 負載模型 → 推演引擎 → 最佳化 → 分析。設定錯誤會丟出 ValueError。
    params：負載模型參數（預設讀 config/load_params.json；情境比較用來換冷氣 COP、熱水器效率）。"""
    house = build_house(settings)
    schedule = Schedule(house, config=settings["schedule"])
    params = copy.deepcopy(params or load_params())
    params["site"]["latitude"], params["site"]["longitude"] = location_coords(settings)   # 太陽位置用所選地區
    load_model = LoadModel(house, schedule, params, adjacency=adjacency(settings))   # 相鄰關係來自平面圖
    simulator = Simulator(house, schedule, load_model, weather=weather_source(settings))
    baseline = ControlPlan(name="基準（一般習慣）", default_setpoint=float(settings["baseline_setpoint_c"]))
    optimizer = Optimizer(simulator, modes_config=settings["modes"], baseline=baseline)
    return Analyzer(optimizer)


# ---------- 冷氣規格 ----------

KW_PER_PING = 0.55   # 冷氣能力估算：每坪約 0.5–0.6 kW（約 450–500 kcal/h，台灣業界常用經驗值）


AUTO_CAPACITY, NO_GRADE = "依功率推算", "未指定"


def capacity_choices(options: list[float]) -> list[str]:
    return [AUTO_CAPACITY, *(f"{o:g} kW" for o in options)]


GRADE_CHOICES = [NO_GRADE, *(f"{g} 級" for g in range(1, 6))]


def _leading_number(value) -> float | None:
    """表格的值 → 數字："3.6 kW" → 3.6、"1 級" → 1、3.6 → 3.6；「依功率推算」、空白 → None。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    head = str(value).split()[0] if str(value).strip() else ""
    try:
        return float(head)
    except ValueError:
        return None


def suggested_capacity_kw(area_ping: float, options: list[float]) -> float:
    """依坪數建議冷氣能力：坪數 × 0.55 kW，取市售規格中不小於此值的最小者。"""
    need = area_ping * KW_PER_PING
    return next((o for o in options if o >= need), options[-1])


def ac_spec_rows(settings: dict, options: list[float]) -> list[dict]:
    """每台冷氣的規格表（介面用）：空間、坪數、建議能力、目前能力、能效等級。"""
    house = build_house(settings)
    rows = []
    for space, a in house.all_appliances():
        if a.id != "air_conditioner":
            continue
        rows.append({"key": f"{space.id}/{a.id}", "空間": space.name, "坪數": space.area_ping,
                     "建議能力(kW)": suggested_capacity_kw(space.area_ping, options),
                     "能力(kW)": f"{a.capacity_kw:g} kW" if a.capacity_kw else AUTO_CAPACITY,
                     "能效等級": f"{a.grade} 級" if a.grade else NO_GRADE})
    return rows


def apply_ac_specs(settings: dict, rows: list[dict]) -> dict:
    """冷氣規格表 → 設定（保留同一台冷氣的功率與啟用設定）。空白代表沿用額定功率推算。"""
    new = copy.deepcopy(settings)
    for r in rows:
        edit = dict(new["appliance_edits"].get(r["key"], {}))
        cap, grade = _leading_number(r.get("能力(kW)")), _leading_number(r.get("能效等級"))
        edit["capacity_kw"] = cap
        edit["grade"] = int(grade) if grade else None
        new["appliance_edits"][r["key"]] = edit
    return new


# ---------- 房型 ----------

ORIENTATION_CODES = {name: code for code, name in ORIENTATION_NAMES.items()}   # 「西」→ "W"

def spaces_table(settings: dict) -> list[dict]:
    """房屋設定 → 介面表格的列（名稱、範本、坪數）。"""
    templates = load_space_templates()
    return [{"id": s["id"], "名稱": s.get("name", templates[s["template"]]["name"]),
             "範本": s["template"], "坪數": float(s.get("area_ping", templates[s["template"]]["default_area_ping"])),
             "窗戶方位": ORIENTATION_NAMES.get(s.get("orientation") or "", "未指定")}
            for s in settings["house"]["spaces"]]


def apply_spaces_table(settings: dict, rows: list[dict]) -> dict:
    """介面表格 → 房屋設定。保留原有空間的家電增減設定；新增的空間依範本建立。"""
    new = copy.deepcopy(settings)
    old = {s["id"]: s for s in settings["house"]["spaces"]}
    used, spaces = set(), []
    for row in rows:
        if not row.get("範本") or not row.get("名稱"):
            continue
        sid = row.get("id") or ""
        if not sid or sid in used:
            n = 1
            while f"{row['範本']}_{n}" in used or f"{row['範本']}_{n}" in old:
                n += 1
            sid = f"{row['範本']}_{n}"
        used.add(sid)
        entry = dict(old.get(sid, {})) if old.get(sid, {}).get("template") == row["範本"] else {}
        entry.update({"id": sid, "name": row["名稱"], "template": row["範本"]})
        if row["範本"] != "utility":
            entry["area_ping"] = float(row["坪數"] or 0)
            orientation = ORIENTATION_CODES.get(row.get("窗戶方位") or "")
            if orientation:
                entry["orientation"] = orientation
            else:
                entry.pop("orientation", None)
        spaces.append(entry)
    new["house"]["spaces"] = spaces
    return new


# ---------- 作息 ----------

ROUTINE_LABELS = {"adult_weekday": "大人平日", "child_weekday": "小孩平日",
                  "adult_holiday": "大人假日", "child_holiday": "小孩假日"}


def routine_times(routine: dict) -> dict[str, str | None]:
    """從作息區塊取出可調整的主要時間：起床、就寢、外出開始、外出結束。"""
    blocks = routine["blocks"]
    wake = next((b["end"] for b in blocks if b["activity"] == "sleep" and b["start"] == "00:00"), None)
    bed = next((b["start"] for b in blocks if b["activity"] == "sleep" and b["end"] == "24:00"), None)
    out = next((b for b in blocks if b["activity"] == "out"), None)
    return {"wake": wake, "bed": bed, "out_start": out["start"] if out else None,
            "out_end": out["end"] if out else None}


def apply_routine_times(settings: dict, routine_id: str, times: dict[str, str]) -> dict:
    """修改某個作息的主要時間（其他區塊、盥洗與洗澡時間沿用預設）。"""
    new = copy.deepcopy(settings)
    blocks = new["schedule"]["routines"][routine_id]["blocks"]
    for b in blocks:
        if b["activity"] == "sleep" and b["start"] == "00:00" and times.get("wake"):
            b["end"] = times["wake"]
        elif b["activity"] == "sleep" and b["end"] == "24:00" and times.get("bed"):
            b["start"] = times["bed"]
    out = next((b for b in blocks if b["activity"] == "out"), None)
    if out is not None and times.get("out_start") and times.get("out_end"):
        out["start"], out["end"] = times["out_start"], times["out_end"]
    return new
