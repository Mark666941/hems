"""介面設定 ↔ 核心模組：把使用者在介面上的修改轉成設定字典，再組裝整個系統。

設計原則：
- 介面只修改「設定字典」，不寫回 config/ 的 JSON 檔，預設值永遠保留。
- 所有設定序列化成一個 JSON 字串，當作快取的 key：設定沒變就不重新計算。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from core.load_model import LoadModel, load_params
from core.metrics import Analyzer
from core.models import House, house_from_config, load_appliance_catalog, load_space_templates
from core.optimizer import Optimizer
from core.schedule import Schedule
from core.simulator import ControlPlan, Simulator, load_baseline_plan

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _read(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def default_settings() -> dict:
    """預設設定（全部來自 config/），介面以這份為起點讓使用者修改。"""
    baseline = load_baseline_plan()
    return {
        "house": _read("default_house.json"),
        "appliance_edits": {},            # "空間id/家電id" → {"power_w": …, "enabled": …}
        "schedule": _read("schedule.json"),
        "modes": _read("modes.json"),
        "baseline_setpoint_c": baseline.default_setpoint,
    }


def to_key(settings: dict) -> str:
    """設定 → 穩定的 JSON 字串（快取 key）。"""
    return json.dumps(settings, ensure_ascii=False, sort_keys=True)


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
    return house


def build_analyzer(settings: dict) -> Analyzer:
    """依設定組裝：房屋 → 作息 → 負載模型 → 推演引擎 → 最佳化 → 分析。設定錯誤會丟出 ValueError。"""
    house = build_house(settings)
    schedule = Schedule(house, config=settings["schedule"])
    load_model = LoadModel(house, schedule, load_params())
    simulator = Simulator(house, schedule, load_model)
    baseline = ControlPlan(name="基準（一般習慣）", default_setpoint=float(settings["baseline_setpoint_c"]))
    optimizer = Optimizer(simulator, modes_config=settings["modes"], baseline=baseline)
    return Analyzer(optimizer)


# ---------- 房型 ----------

def spaces_table(settings: dict) -> list[dict]:
    """房屋設定 → 介面表格的列（名稱、範本、坪數）。"""
    templates = load_space_templates()
    return [{"id": s["id"], "名稱": s.get("name", templates[s["template"]]["name"]),
             "範本": s["template"], "坪數": float(s.get("area_ping", templates[s["template"]]["default_area_ping"]))}
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
