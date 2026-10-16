"""智慧平面圖：格局、家電擺放、遙控器控制（Python 端）。

前端（ui/floorplan_frontend/index.html）負責繪圖與拖曳、遙控器按鈕；
這裡負責：
- 格局：房間位置與尺寸（公尺），尺寸改變時同步更新模擬用的坪數
- 家電擺放：新增、移除、拖到別的房間（會改變房屋設定，模擬隨之重算）
- 遙控器：把手動操作轉成 ControlPlan 覆寫（從操作的那一小時起到當天結束）
- 把模擬結果整理成平面圖要顯示的資料（各房間室溫、各家電即時用電）
"""
from __future__ import annotations

import copy
import math
from dataclasses import replace
from pathlib import Path

import streamlit.components.v1 as components

from core.models import House
from core.simulator import ControlPlan, SimulationResult

FRONTEND_DIR = Path(__file__).resolve().parent / "floorplan_frontend"
_component = components.declare_component("hems_floorplan", path=str(FRONTEND_DIR))

PING_TO_M2 = 3.3058
ADDABLE = {"air_conditioner": "冷氣", "lighting": "電燈", "dehumidifier": "除濕機", "refrigerator": "冰箱"}
TEMPLATE_ORDER = ["bedroom", "living_room", "kitchen", "bathroom", "utility"]
UTILITY_SIZE = (1.8, 1.4)          # 公共設備（陽台）沒有面積，只為了顯示給一個尺寸
SNAP_M = 0.05


# ---------- 格局 ----------

def _default_width(area_m2: float, template: str) -> float:
    """依面積給一個合理的房間寬度（0.1 m 為單位），深度 = 面積 ÷ 寬度，面積完全不變。"""
    if area_m2 <= 0:
        return UTILITY_SIZE[0]
    ratio = 1.0 if template == "bathroom" else 1.12
    return max(1.2, round(math.sqrt(area_m2 * ratio), 1))


def room_dims(space_cfg: dict, layout_room: dict | None, templates: dict) -> tuple[float, float]:
    """房間寬、深（m）。"""
    area_m2 = float(space_cfg.get("area_ping", templates[space_cfg["template"]]["default_area_ping"])) * PING_TO_M2
    if area_m2 <= 0:
        return UTILITY_SIZE
    width = (layout_room or {}).get("w") or _default_width(area_m2, space_cfg["template"])
    return width, area_m2 / width


def auto_layout(settings: dict, templates: dict) -> dict[str, dict]:
    """自動排列房間：依面積由大到小，輪流放進目前最窄的一排，讓整體接近矩形；
    每排內再依房間類型排序（臥室、客廳、廚房、浴室、公共設備）。"""
    spaces = settings["house"]["spaces"]
    sized = [(s, *room_dims(s, None, templates)) for s in spaces]
    n_rows = 2 if len(spaces) <= 8 else 3
    rows: list[list] = [[] for _ in range(n_rows)]
    widths = [0.0] * n_rows
    for item in sorted(sized, key=lambda t: -(t[1] * t[2])):
        i = widths.index(min(widths))
        rows[i].append(item)
        widths[i] += item[1]
    layout, y = {}, 0.0
    for row in rows:
        row.sort(key=lambda t: TEMPLATE_ORDER.index(t[0]["template"]) if t[0]["template"] in TEMPLATE_ORDER else 99)
        x = 0.0
        for s, w, d in row:
            layout[s["id"]] = {"x": round(x, 2), "y": round(y, 2), "w": w}
            x += w
        y += max((d for _, _, d in row), default=0)
    return layout


def ensure_layout(settings: dict, house: House, templates: dict) -> dict:
    """補齊格局：沒有位置的房間用自動排列，沒有位置的家電放在房間的預設點。不修改原設定。"""
    layout = copy.deepcopy(settings.get("layout") or {"rooms": {}, "appliances": {}})
    ids = [s["id"] for s in settings["house"]["spaces"]]
    if any(i not in layout["rooms"] for i in ids):
        auto = auto_layout(settings, templates)
        for i in ids:
            layout["rooms"].setdefault(i, auto[i])
    layout["rooms"] = {i: layout["rooms"][i] for i in ids}
    cfg = {s["id"]: s for s in settings["house"]["spaces"]}
    for space in house.spaces:
        x, y = layout["rooms"][space.id]["x"], layout["rooms"][space.id]["y"]
        w, d = room_dims(cfg[space.id], layout["rooms"][space.id], templates)
        for a in space.appliances:
            if a.id in ADDABLE:
                layout["appliances"].setdefault(f"{space.id}/{a.id}", default_spot(a.id, x, y, w, d))
    keys = {f"{s.id}/{a.id}" for s, a in house.all_appliances() if a.id in ADDABLE}
    layout["appliances"] = {k: v for k, v in layout["appliances"].items() if k in keys}
    return layout


def default_spot(appliance_id: str, x: float, y: float, w: float, d: float) -> dict:
    """家電的預設位置（避開房間中央的名稱與室溫標籤）：
    冷氣掛在上方牆面中央、電燈在右上、除濕機在右下、冰箱在左下。"""
    if appliance_id == "air_conditioner":
        return {"x": round(x + w / 2, 2), "y": round(y + min(0.5, d * 0.18), 2)}
    spots = {"lighting": (0.80, 0.24), "dehumidifier": (0.80, 0.74), "refrigerator": (0.20, 0.74)}
    fx, fy = spots[appliance_id]
    return {"x": round(x + w * fx, 2), "y": round(y + d * fy, 2)}


def room_at(layout: dict, settings: dict, templates: dict, x: float, y: float) -> str | None:
    cfg = {s["id"]: s for s in settings["house"]["spaces"]}
    for sid, r in layout["rooms"].items():
        w, d = room_dims(cfg[sid], r, templates)
        if r["x"] <= x <= r["x"] + w and r["y"] <= y <= r["y"] + d:
            return sid
    return None


def set_room_size(settings: dict, space_id: str, width_m: float, depth_m: float) -> dict:
    """修改房間尺寸：寬記在格局，面積（坪）寫回房屋設定，模擬會用新的面積重算。"""
    new = copy.deepcopy(settings)
    new.setdefault("layout", {"rooms": {}, "appliances": {}})
    new["layout"]["rooms"].setdefault(space_id, {"x": 0, "y": 0})["w"] = round(width_m, 2)
    for s in new["house"]["spaces"]:
        if s["id"] == space_id:
            s["area_ping"] = round(width_m * depth_m / PING_TO_M2, 3)
    return new


# ---------- 家電擺放 ----------

def _space_cfg(settings: dict, space_id: str) -> dict:
    return next(s for s in settings["house"]["spaces"] if s["id"] == space_id)


def add_appliance(settings: dict, house: House, space_id: str, appliance_id: str,
                  power_w: float | None = None, spot: dict | None = None) -> tuple[dict, str]:
    """在房間新增家電。回傳（新設定, 訊息）；同一房間同類家電只能一台。"""
    space = house.get_space(space_id)
    if space.get_appliance(appliance_id) is not None:
        return settings, f"{space.name}已經有{ADDABLE[appliance_id]}了"
    if appliance_id == "air_conditioner" and space.area_ping <= 0:
        return settings, f"{space.name}沒有室內面積，不能裝冷氣"
    new = copy.deepcopy(settings)
    cfg = _space_cfg(new, space_id)
    removed = cfg.get("remove_appliances", [])
    if appliance_id in removed:
        # 房間範本原本就有、之前被移除 → 取消移除；搬過來的家電保留原功率
        removed.remove(appliance_id)
        if power_w:
            cfg.setdefault("appliance_overrides", {}).setdefault(appliance_id, {})["power_w"] = power_w
    else:
        cfg.setdefault("add_appliances", []).append({"id": appliance_id, "power_w": power_w} if power_w else appliance_id)
    if spot:
        new.setdefault("layout", {"rooms": {}, "appliances": {}})["appliances"][f"{space_id}/{appliance_id}"] = spot
    return new, f"已在{space.name}新增{ADDABLE[appliance_id]}"


def _from_template(settings: dict, space_id: str, appliance_id: str) -> bool:
    from core.models import load_space_templates
    cfg = _space_cfg(settings, space_id)
    template = load_space_templates()[cfg["template"]]
    ids = [e if isinstance(e, str) else e["id"] for e in template["appliances"]]
    return appliance_id in ids and appliance_id not in cfg.get("remove_appliances", [])


def remove_appliance(settings: dict, house: House, space_id: str, appliance_id: str) -> tuple[dict, str]:
    new = copy.deepcopy(settings)
    cfg = _space_cfg(new, space_id)
    added = cfg.get("add_appliances", [])
    kept = [e for e in added if (e if isinstance(e, str) else e["id"]) != appliance_id]
    if len(kept) != len(added):
        cfg["add_appliances"] = kept
    if _from_template(new, space_id, appliance_id):
        cfg.setdefault("remove_appliances", []).append(appliance_id)
    key = f"{space_id}/{appliance_id}"
    new.get("layout", {}).get("appliances", {}).pop(key, None)
    new.get("appliance_edits", {}).pop(key, None)
    return new, f"已移除{house.get_space(space_id).name}的{ADDABLE.get(appliance_id, appliance_id)}"


def move_appliance(settings: dict, house: House, layout: dict, templates: dict,
                   key: str, x: float, y: float) -> tuple[dict, str | None]:
    """拖曳家電：同一房間只更新位置；拖到別的房間則把家電搬過去（保留功率）。"""
    space_id, appliance_id = key.split("/")
    x, y = round(x / SNAP_M) * SNAP_M, round(y / SNAP_M) * SNAP_M
    target = room_at(layout, settings, templates, x, y)
    new = copy.deepcopy(settings)
    new.setdefault("layout", copy.deepcopy(layout))
    if target is None or target == space_id:
        if target is None:                            # 拖到走道：留在原房間
            return settings, "請把家電放在房間內"
        new["layout"]["appliances"][key] = {"x": x, "y": y}
        return new, None
    appliance = house.get_space(space_id).get_appliance(appliance_id)
    new, msg = add_appliance(new, house, target, appliance_id, appliance.power_w, {"x": x, "y": y})
    if msg.startswith("已在"):
        new, _ = remove_appliance(new, house, space_id, appliance_id)
        new["layout"]["appliances"][f"{target}/{appliance_id}"] = {"x": x, "y": y}
        return new, f"已將{ADDABLE[appliance_id]}從{house.get_space(space_id).name}移到{house.get_space(target).name}"
    return settings, msg


def move_room(settings: dict, layout: dict, space_id: str, x: float, y: float) -> dict:
    """拖曳房間（調整格局）：房間內的家電跟著平移。"""
    new = copy.deepcopy(settings)
    new["layout"] = copy.deepcopy(layout)
    old = new["layout"]["rooms"][space_id]
    x, y = round(x / 0.1) * 0.1, round(y / 0.1) * 0.1
    dx, dy = x - old["x"], y - old["y"]
    old["x"], old["y"] = round(x, 2), round(y, 2)
    for key, pos in new["layout"]["appliances"].items():
        if key.startswith(space_id + "/"):
            pos["x"], pos["y"] = round(pos["x"] + dx, 2), round(pos["y"] + dy, 2)
    return new


# ---------- 遙控器 ----------

def apply_remote(plan: ControlPlan, remote: dict[str, dict]) -> ControlPlan:
    """把遙控器操作疊加到控制策略上：從操作的那一小時起，到當天結束都依手動設定。

    remote：{"空間id/家電id": {"on": bool, "setpoint": float | None, "level": float | None, "from_hour": int}}
    """
    new = replace(plan, setpoints={k: list(v) for k, v in plan.setpoints.items()},
                  ac_on={k: list(v) for k, v in plan.ac_on.items()},
                  appliance_forced={k: {a: list(v) for a, v in d.items()} for k, d in plan.appliance_forced.items()})
    for key, cmd in remote.items():
        space_id, appliance_id = key.split("/")
        hours = range(int(cmd["from_hour"]), 24)
        if appliance_id == "air_conditioner":
            on = new.ac_on.setdefault(space_id, [0.0] * 24)
            sp = new.setpoints.setdefault(space_id, [plan.default_setpoint] * 24)
            for h in hours:
                on[h] = 1.0 if cmd["on"] else 0.0
                if cmd.get("setpoint") is not None:
                    sp[h] = float(cmd["setpoint"])
        else:
            forced = new.appliance_forced.setdefault(space_id, {}).setdefault(appliance_id, [None] * 24)
            for h in hours:
                forced[h] = (cmd.get("level") or 1.0) if cmd["on"] else 0.0
    return new


def remote_command(remote: dict, key: str, command: str, value, state: dict, hour: int) -> dict:
    """處理一個遙控器按鈕。state 為家電目前狀態（on、setpoint、level），回傳新的 remote 字典。"""
    new = copy.deepcopy(remote)
    cmd = new.get(key, {"on": state["on"], "setpoint": state.get("setpoint"), "level": state.get("level")})
    cmd["from_hour"] = hour
    if command == "toggle":
        cmd["on"] = not cmd["on"]
    elif command in ("temp_up", "temp_down"):
        base = cmd.get("setpoint") or state.get("setpoint") or 26.0
        cmd["setpoint"] = min(30.0, max(16.0, base + (0.5 if command == "temp_up" else -0.5)))
        cmd["on"] = True
    elif command == "level":
        cmd["level"], cmd["on"] = float(value), True
    elif command == "auto":
        new.pop(key, None)
        return new
    new[key] = cmd
    return new


# ---------- 顯示資料 ----------

def build_payload(settings: dict, house: House, layout: dict, templates: dict, result: SimulationResult,
                  plan: ControlPlan, remote: dict, hour: int, status: dict, comfort: dict) -> dict:
    """整理平面圖需要的資料（全部是 JSON 可序列化的基本型別）。"""
    cfg = {s["id"]: s for s in settings["house"]["spaces"]}
    climate = result.climate.set_index(["space_id", "hour"])
    records = result.records
    now = records[records["hour"] == hour].set_index(["space_id", "appliance_id"])["kwh"]
    today = records.groupby(["space_id", "appliance_id"])["kwh"].sum()
    rooms, appliances = [], []
    for space in house.spaces:
        r = layout["rooms"][space.id]
        w, d = room_dims(cfg[space.id], r, templates)
        temp = float(climate.loc[(space.id, hour), "room_temp_c"]) if (space.id, hour) in climate.index else None
        others = [a.name for a in space.appliances if a.id not in ADDABLE and a.enabled]
        rooms.append({"id": space.id, "name": space.name, "template": space.template, "x": r["x"], "y": r["y"],
                      "w": round(w, 2), "d": round(d, 2), "area_ping": round(space.area_ping, 1),
                      "area_m2": round(w * d, 1) if space.area_ping > 0 else None,
                      "temp": round(temp, 1) if temp is not None else None,
                      "comfort": comfort.get(space.id), "others": others})
        for a in space.appliances:
            if a.id not in ADDABLE:
                continue
            key = f"{space.id}/{a.id}"
            pos = layout["appliances"][key]
            watts = float(now.get((space.id, a.id), 0.0)) * 1000
            state = {"on": watts > 0.5, "setpoint": None, "level": None}
            if a.id == "air_conditioner":
                on_frac = plan.ac_on.get(space.id, [0.0] * 24)[hour]
                state["on"] = on_frac > 0
                state["setpoint"] = plan.setpoints.get(space.id, [plan.default_setpoint] * 24)[hour]
            manual = remote.get(key)
            if manual and manual["from_hour"] <= hour:
                state.update(on=manual["on"], level=manual.get("level"))
                if manual.get("setpoint") is not None:
                    state["setpoint"] = manual["setpoint"]
            appliances.append({
                "key": key, "type": a.id, "name": a.name, "room": space.name, "x": pos["x"], "y": pos["y"],
                "watts": round(watts), "rated_w": a.power_w, "kwh_today": round(float(today.get((space.id, a.id), 0.0)), 2),
                "on": bool(state["on"]), "setpoint": state["setpoint"], "level": state["level"],
                "manual": bool(manual and manual["from_hour"] <= hour),
                "manual_from": manual["from_hour"] if manual else None,
                "essential": a.load_type.value == "essential", "room_temp": rooms[-1]["temp"],
            })
    return {"rooms": rooms, "appliances": appliances, "status": status, "addable": ADDABLE}


def floorplan(payload: dict, key: str = "floorplan"):
    """顯示平面圖元件，回傳使用者最後一次操作（拖曳、遙控器）的事件。"""
    return _component(data=payload, key=key, default=None)
