"""家庭成員：平面圖上的人物（位置依作息移動）、個人作息編輯、新增／刪除成員、臨時變動。

- 位置：作息模型（core/schedule.py）算出每位成員每分鐘在哪個空間、做什麼；這裡取每小時停留最久的空間。
- 個人作息：預設大人（或小孩）共用同一份作息範本；修改某一個人時，先為他複製一份專屬作息再改，不影響其他人。
- 臨時變動：在平面圖拖曳人物到別的房間＝「這個小時他改在這裡」，只存在這一天（不寫入設定檔），
  模擬時以額外的作息區塊疊加上去。
"""
from __future__ import annotations

import copy
from collections import Counter

from core.models import House
from core.schedule import Schedule
from ui.settings import apply_routine_times, routine_times

MEMBER_COLORS = ["#3ba7ff", "#f472b6", "#22c55e", "#f59e0b", "#a78bfa", "#14b8a6", "#f97316", "#e11d48"]
ACTIVITY_ICONS = {"sleep": "💤", "wash": "🪥", "shower": "🚿", "cook": "🍳", "meal": "🍚", "leisure": "📺",
                  "study": "📚", "rest": "🛋️", "out": "🚶"}
ROLE_LABELS = {"adult": "大人", "child": "小孩"}
NEW_MEMBER_BLOCKS = {   # 新成員的盥洗、洗澡時段（洗澡次數會影響熱水器用電）
    "adult": {"weekday": [{"start": "07:15", "end": "07:25", "space": "@bathroom", "activity": "wash"},
                          {"start": "22:30", "end": "23:00", "space": "@bathroom", "activity": "shower"}],
              "holiday": [{"start": "08:50", "end": "09:00", "space": "@bathroom", "activity": "wash"},
                          {"start": "22:30", "end": "23:00", "space": "@bathroom", "activity": "shower"}]},
    "child": {"weekday": [{"start": "07:00", "end": "07:10", "space": "@bathroom", "activity": "wash"},
                          {"start": "21:30", "end": "22:00", "space": "@bathroom", "activity": "shower"}],
              "holiday": [{"start": "08:30", "end": "08:40", "space": "@bathroom", "activity": "wash"},
                          {"start": "21:30", "end": "22:00", "space": "@bathroom", "activity": "shower"}]},
}


def member_color(index: int) -> str:
    return MEMBER_COLORS[index % len(MEMBER_COLORS)]


def hour_location(schedule: Schedule, member, day_type: str, hour: int) -> tuple[str | None, str]:
    """這個小時停留最久的（空間, 活動）；空間為 None 代表外出。"""
    minutes = schedule.member_timeline(member, day_type)[hour * 60:(hour + 1) * 60]
    (space, activity), _ = Counter((s.space, s.activity) for s in minutes).most_common(1)[0]
    return space, activity


def person_states(schedule: Schedule, house: House, day_type: str, hour: int, overrides: dict) -> list[dict]:
    """平面圖要顯示的人物：位置、活動、顏色、是否為臨時變動。"""
    names = {s.id: s.name for s in house.spaces}
    labels = schedule.activities
    people = []
    for i, m in enumerate(schedule.members):
        space, activity = hour_location(schedule, m, day_type, hour)
        people.append({
            "id": m.id, "name": m.name, "initial": m.name[:1], "role": m.role,
            "role_label": ROLE_LABELS.get(m.role, m.role), "color": member_color(i),
            "room": space, "room_name": names.get(space, "外出"), "activity": activity,
            "activity_label": labels.get(activity, activity), "icon": ACTIVITY_ICONS.get(activity, ""),
            "sleeping": activity == "sleep", "temporary": f"{m.id}@{hour}" in overrides,
            "timeline": day_timeline(schedule, m, day_type, names),
            "times": routine_times(schedule.routines[m.routines[day_type]]),
            "bedroom": m.bedroom,
        })
    return people


def day_timeline(schedule: Schedule, member, day_type: str, names: dict) -> list[dict]:
    """一整天 24 小時的位置與活動（人物面板的時間軸用）。"""
    labels = schedule.activities
    out = []
    for h in range(24):
        space, activity = hour_location(schedule, member, day_type, h)
        out.append({"hour": h, "room": names.get(space, "外出"), "activity": activity,
                    "label": labels.get(activity, activity)})
    return out


# ---------- 修改成員（寫入設定檔） ----------

def _member(settings: dict, member_id: str) -> dict:
    for m in settings["schedule"]["members"]:
        if m["id"] == member_id:
            return m
    raise KeyError(f"找不到成員：{member_id}")


def set_member_times(settings: dict, member_id: str, day_type: str, times: dict) -> dict:
    """只修改這位成員的作息：作息範本若和別人共用，先複製一份專屬的再改。"""
    new = copy.deepcopy(settings)
    member = _member(new, member_id)
    routine_id = member["routines"][day_type]
    shared = sum(1 for m in new["schedule"]["members"] if m["routines"][day_type] == routine_id) > 1
    if shared:
        own_id = f"{member_id}_{day_type}"
        own = copy.deepcopy(new["schedule"]["routines"][routine_id])
        own["name"] = f"{member['name']}的{'平日' if day_type == 'weekday' else '假日'}作息"
        new["schedule"]["routines"][own_id] = own
        member["routines"][day_type] = routine_id = own_id
    return apply_routine_times(new, routine_id, times)


def rename_member(settings: dict, member_id: str, name: str) -> dict:
    new = copy.deepcopy(settings)
    _member(new, member_id)["name"] = name.strip() or _member(settings, member_id)["name"]
    return new


def add_member(settings: dict, house: House, name: str, role: str, bedroom: str) -> tuple[dict, str]:
    """新增成員：套用大人／小孩的作息範本與盥洗、洗澡時段，浴室用第一間浴室。"""
    bathrooms = [s.id for s in house.spaces if s.template == "bathroom"]
    if not bathrooms:
        return settings, "房屋沒有浴室，無法新增成員"
    new = copy.deepcopy(settings)
    ids = {m["id"] for m in new["schedule"]["members"]}
    n = 1
    while f"member_{n}" in ids:
        n += 1
    new["schedule"]["members"].append({
        "id": f"member_{n}", "name": name.strip() or f"成員 {n}", "role": role,
        "bedroom": bedroom, "bathroom": bathrooms[0],
        "routines": {"weekday": f"{role}_weekday", "holiday": f"{role}_holiday"},
        "extra_blocks": copy.deepcopy(NEW_MEMBER_BLOCKS[role]),
    })
    return new, f"已新增{ROLE_LABELS[role]}「{name.strip() or f'成員 {n}'}」"


def remove_member(settings: dict, member_id: str) -> tuple[dict, str]:
    if len(settings["schedule"]["members"]) <= 1:
        return settings, "至少要保留一位家庭成員"
    new = copy.deepcopy(settings)
    name = _member(new, member_id)["name"]
    new["schedule"]["members"] = [m for m in new["schedule"]["members"] if m["id"] != member_id]
    return new, f"已刪除成員「{name}」"


# ---------- 臨時變動（只在今天、不寫入設定檔） ----------

def move_person(overrides: dict, member_id: str, hour: int, space_id: str, activity: str) -> dict:
    """拖曳人物到別的房間：這個小時改在那裡（外出中拖進屋內視為在家休閒）。"""
    new = dict(overrides)
    new[f"{member_id}@{hour}"] = {"member": member_id, "hour": hour, "space": space_id,
                                  "activity": "leisure" if activity == "out" else activity}
    return new


def apply_person_overrides(settings: dict, overrides: dict, day_type: str) -> dict:
    """把臨時變動疊加成額外的作息區塊（後套用的區塊會蓋過原本的作息）。"""
    if not overrides:
        return settings
    new = copy.deepcopy(settings)
    members = {m["id"]: m for m in new["schedule"]["members"]}
    for o in overrides.values():
        member = members.get(o["member"])
        if member is None:
            continue
        h = int(o["hour"])
        block = {"start": f"{h:02d}:00", "end": f"{h + 1:02d}:00" if h < 23 else "24:00",
                 "space": o["space"], "activity": o["activity"]}
        member.setdefault("extra_blocks", {}).setdefault(day_type, []).append(block)
    return new
