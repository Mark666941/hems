"""智慧平面圖：格局、家電擺放、遙控器（Python 端邏輯）。"""
import json
from datetime import date

import pytest

from core.models import load_space_templates
from ui.floorplan import (PING_TO_M2, add_appliance, apply_remote, auto_layout, build_payload, ensure_layout,
                          move_appliance, move_room, remote_command, remove_appliance, room_at, room_dims,
                          set_room_size)
from ui.settings import build_analyzer, default_settings, sim_key

T = load_space_templates()
DAY = date(2026, 7, 20)


@pytest.fixture(scope="module")
def base():
    s = default_settings()
    a = build_analyzer(s)
    return s, a, ensure_layout(s, a.sim.house, T)


def _cfg(s, sid):
    return next(x for x in s["house"]["spaces"] if x["id"] == sid)


# ---------- 格局 ----------

def test_room_dims_keep_area(base):
    s, a, layout = base
    for space in a.sim.house.spaces:
        if space.area_ping > 0:
            w, d = room_dims(_cfg(s, space.id), layout["rooms"][space.id], T)
            assert w * d == pytest.approx(space.area_ping * PING_TO_M2)   # 尺寸換算不改變面積（模擬結果不變）


def test_auto_layout_rooms_do_not_overlap(base):
    s, _, _ = base
    layout = auto_layout(s, T)
    rects = []
    for sid, r in layout.items():
        w, d = room_dims(_cfg(s, sid), r, T)
        rects.append((r["x"], r["y"], r["x"] + w, r["y"] + d))
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            overlap_w = min(a[2], b[2]) - max(a[0], b[0])
            overlap_h = min(a[3], b[3]) - max(a[1], b[1])
            assert overlap_w <= 1e-6 or overlap_h <= 1e-6


def test_every_controllable_appliance_is_inside_its_room(base):
    s, a, layout = base
    for key, pos in layout["appliances"].items():
        assert room_at(layout, s, T, pos["x"], pos["y"]) == key.split("/")[0]


def test_set_room_size_updates_area():
    s = set_room_size(default_settings(), "master_bedroom", 5.0, 4.0)
    assert _cfg(s, "master_bedroom")["area_ping"] == pytest.approx(20 / PING_TO_M2, abs=1e-3)
    assert build_analyzer(s).sim.house.get_space("master_bedroom").area_ping == pytest.approx(20 / PING_TO_M2, abs=1e-3)


def test_layout_positions_do_not_change_simulation_key(base):
    s, _, layout = base
    assert sim_key(s) == sim_key(move_room({**s, "layout": layout}, layout, "kitchen", 20, 20))


# ---------- 家電擺放 ----------

def test_move_appliance_to_other_room_keeps_power(base):
    s, a, layout = base
    r = layout["rooms"]["kitchen"]
    w, d = room_dims(_cfg(s, "kitchen"), r, T)
    new, msg = move_appliance({**s, "layout": layout}, a.sim.house, layout, T, "living_room/air_conditioner",
                              r["x"] + w / 2, r["y"] + d / 2)
    assert "移到廚房" in msg
    house = build_analyzer(new).sim.house
    assert house.get_space("kitchen").get_appliance("air_conditioner").power_w == 1500
    assert house.get_space("living_room").get_appliance("air_conditioner") is None


def test_move_rejected_when_target_has_same_type(base):
    s, a, layout = base
    r = layout["rooms"]["bedroom_2"]
    new, msg = move_appliance({**s, "layout": layout}, a.sim.house, layout, T, "living_room/air_conditioner",
                              r["x"] + 1, r["y"] + 1)
    assert "已經有冷氣" in msg and new is not None


def test_add_and_remove_round_trip(base):
    s, a, layout = base
    new, _ = remove_appliance({**s, "layout": layout}, a.sim.house, "bathroom_2", "lighting")
    assert build_analyzer(new).sim.house.get_space("bathroom_2").get_appliance("lighting") is None
    new2, msg = add_appliance(new, build_analyzer(new).sim.house, "bathroom_2", "lighting")
    assert msg.startswith("已在")
    assert build_analyzer(new2).sim.house.get_space("bathroom_2").get_appliance("lighting") is not None
    _, msg3 = add_appliance(s, a.sim.house, "utility", "air_conditioner")
    assert "不能裝冷氣" in msg3


# ---------- 遙控器 ----------

def test_remote_precooling(base):
    """17:00 遠端開主臥冷氣：從 17 時起冷氣運轉、室溫降到設定值。"""
    _, a, _ = base
    opt = a.opt.optimize(DAY, "normal")
    remote = remote_command({}, "master_bedroom/air_conditioner", "toggle", None, {"on": False, "setpoint": 26.5}, 17)
    result = a.sim.run(DAY, "tou_2", apply_remote(opt.plan, remote))
    ac = result.records.query("space_id == 'master_bedroom' and appliance_id == 'air_conditioner'")
    assert ac[ac["hour"] == 17]["kwh"].sum() > 0
    temps = result.climate.query("space_id == 'master_bedroom'").set_index("hour")["room_temp_c"]
    assert temps[18] < 27
    assert opt.plan.ac_on["master_bedroom"][17] == 0       # 原本的計畫不被修改


def test_remote_light_off_and_level():
    plan_hours = remote_command({}, "living_room/lighting", "toggle", None, {"on": True}, 19)
    assert plan_hours["living_room/lighting"]["on"] is False
    dim = remote_command({}, "living_room/lighting", "level", 0.5, {"on": False}, 19)
    assert dim["living_room/lighting"] == {"on": True, "setpoint": None, "level": 0.5, "from_hour": 19}
    assert remote_command(dim, "living_room/lighting", "auto", None, {"on": True}, 19) == {}


def test_remote_forced_light_runs_even_when_room_empty(base):
    _, a, _ = base
    plan = a.opt.optimize(DAY, "normal").plan
    remote = remote_command({}, "living_room/lighting", "level", 0.5, {"on": False}, 10)
    result = a.sim.run(DAY, "tou_2", apply_remote(plan, remote))
    light = result.records.query("space_id == 'living_room' and appliance_id == 'lighting'").set_index("hour")["kwh"]
    assert light[10] == pytest.approx(0.060 * 0.5)          # 客廳燈 60 W × 50%，即使白天沒人


def test_payload_is_json_and_complete(base):
    s, a, layout = base
    opt = a.opt.optimize(DAY, "normal")
    status = {"time": "17:00"}
    payload = build_payload(s, a.sim.house, layout, T, opt.result, opt.plan, {}, 17, status, {})
    json.dumps(payload, ensure_ascii=False)
    assert len(payload["rooms"]) == 8
    types = {x["type"] for x in payload["appliances"]}
    assert types == {"air_conditioner", "lighting", "dehumidifier", "refrigerator"}
    fridge = next(x for x in payload["appliances"] if x["type"] == "refrigerator")
    assert fridge["on"] and fridge["essential"] and fridge["watts"] == 50
