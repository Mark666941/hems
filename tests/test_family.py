"""第 4 批：平面圖上的家庭成員（位置、個人作息、新增／刪除、臨時變動）。"""
from datetime import date

import pytest

from core.schedule import Schedule
from ui.family import (add_member, apply_person_overrides, hour_location, move_person, person_states, remove_member,
                       rename_member, set_member_times)
from ui.settings import build_analyzer, build_house, default_settings, routine_times

DAY = date(2026, 7, 20)   # 週一


@pytest.fixture
def setup():
    s = default_settings()
    house = build_house(s)
    return s, house, Schedule(house, config=s["schedule"])


def test_default_names(setup):
    _, _, sched = setup
    assert [m.name for m in sched.members] == ["爸爸", "媽媽", "哥哥", "妹妹"]


def test_people_follow_routine(setup):
    _, house, sched = setup
    night = {p["id"]: p for p in person_states(sched, house, "weekday", 3, {})}
    assert night["parent_1"]["room"] == "master_bedroom" and night["parent_1"]["sleeping"]
    assert night["child_1"]["room"] == "bedroom_1" and night["child_2"]["room"] == "bedroom_2"
    noon = person_states(sched, house, "weekday", 12, {})
    assert all(p["room"] is None and p["room_name"] == "外出" for p in noon)        # 平日中午全家外出
    dinner = person_states(sched, house, "weekday", 19, {})
    assert {p["room"] for p in dinner} == {"kitchen"}                             # 19 時一起吃晚餐


def test_person_state_fields(setup):
    _, house, sched = setup
    p = person_states(sched, house, "weekday", 20, {})[0]
    assert len(p["timeline"]) == 24 and p["color"].startswith("#") and p["initial"] == "爸"
    assert p["times"] == {"wake": "07:00", "bed": "23:00", "out_start": "08:00", "out_end": "18:00"}


def test_individual_times_do_not_change_others(setup):
    s, house, _ = setup
    new = set_member_times(s, "parent_1", "weekday", {"wake": "06:00", "out_start": "07:00", "out_end": "17:00"})
    sched = Schedule(house, config=new["schedule"])
    dad, mom = sched.members[0], sched.members[1]
    assert routine_times(sched.routines[dad.routines["weekday"]])["out_end"] == "17:00"
    assert routine_times(sched.routines[mom.routines["weekday"]])["out_end"] == "18:00"   # 媽媽不受影響
    assert hour_location(sched, dad, "weekday", 17)[0] is not None    # 爸爸 17 時已到家
    assert hour_location(sched, mom, "weekday", 17)[0] is None


def test_rename_add_remove(setup):
    s, house, _ = setup
    s = rename_member(s, "child_1", "小明")
    assert s["schedule"]["members"][2]["name"] == "小明"
    s, msg = add_member(s, house, "阿嬤", "adult", "bedroom_2")
    assert "阿嬤" in msg and len(s["schedule"]["members"]) == 5
    a = build_analyzer(s)                                            # 新成員的作息有效、可以模擬
    assert a.sim.schedule.people_at_home("weekday")[3] == 5
    tasks = {t["appliance"]: t for t in a.sim.load_model.shiftable_tasks(a.sim.weather.get_hourly_weather(DAY), "weekday")}
    base = {t["appliance"]: t for t in build_analyzer(default_settings()).sim.load_model.shiftable_tasks(
        a.sim.weather.get_hourly_weather(DAY), "weekday")}
    assert tasks["water_heater"]["energy_kwh"] > base["water_heater"]["energy_kwh"]   # 多一人洗澡，熱水用電增加
    s, _ = remove_member(s, "member_1")
    assert len(s["schedule"]["members"]) == 4


def test_cannot_remove_last_member():
    s = default_settings()
    s["schedule"]["members"] = s["schedule"]["members"][:1]
    new, msg = remove_member(s, s["schedule"]["members"][0]["id"])
    assert new is s and "至少" in msg


def test_temporary_move_changes_simulation(setup):
    """哥哥 20 時改在客廳（臨時變動）：客廳該小時人數增加，設定檔本身不變。"""
    s, house, sched = setup
    overrides = move_person({}, "child_1", 20, "living_room", "leisure")
    moved = apply_person_overrides(s, overrides, "weekday")
    assert s == default_settings()                                       # 原設定不被修改
    before = Schedule(house, config=s["schedule"]).occupancy("weekday")
    after = Schedule(house, config=moved["schedule"]).occupancy("weekday")
    assert after.loc[20, "living_room"] > before.loc[20, "living_room"]
    assert after.loc[20, "bathroom_2"] < before.loc[20, "bathroom_2"]  # 原本 20:30 要洗澡
    states = person_states(Schedule(house, config=moved["schedule"]), house, "weekday", 20, overrides)
    kid = next(p for p in states if p["id"] == "child_1")
    assert kid["room"] == "living_room" and kid["temporary"]
