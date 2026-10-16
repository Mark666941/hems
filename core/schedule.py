"""作息模型：把家庭成員的作息轉成「24 小時 × 各空間」的使用人數。

做法：
1. 每位成員一天切成 1440 分鐘，每分鐘記錄「在哪個空間、做什麼」。
2. 先用作息的 default 填滿整天，再依序套用 blocks、成員自己的 extra_blocks。
3. 每小時統計各空間的分鐘數 ÷ 60 = 該小時平均人數。
   例如 07:30 出門，07 時那一小時只在家 30 分鐘，算 0.5 人。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from core.models import House, Member

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
MINUTES_PER_DAY = 24 * 60
DAY_TYPES = ("weekday", "holiday")


def _to_minute(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    if not (0 <= h <= 24 and 0 <= m < 60) or h * 60 + m > MINUTES_PER_DAY:
        raise ValueError(f"時間格式錯誤：{hhmm}")
    return h * 60 + m


@dataclass
class Slot:
    """某一分鐘的狀態；space 為 None 代表外出。"""

    space: str | None
    activity: str


class Schedule:
    """讀取 schedule.json，計算每位成員每分鐘的位置與活動。"""

    def __init__(self, house: House, path: Path = CONFIG_DIR / "schedule.json"):
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
        self.house = house
        self.activities: dict[str, str] = config["activities"]
        self.routines: dict[str, dict] = config["routines"]
        self.members = [
            Member(
                id=m["id"], name=m["name"], role=m["role"],
                bedroom=m["bedroom"], bathroom=m["bathroom"],
                routines=m["routines"], extra_blocks=m.get("extra_blocks", {}),
            )
            for m in config["members"]
        ]
        # 作息只和「平日／假日」有關，計算結果可以重複使用（整月推演時會呼叫數千次）
        self._cache: dict[tuple, object] = {}
        self._validate()

    # ---------- 檢查設定 ----------

    def _validate(self) -> None:
        space_ids = {s.id for s in self.house.spaces}
        for m in self.members:
            for sid in (m.bedroom, m.bathroom):
                if sid not in space_ids:
                    raise ValueError(f"{m.name} 的空間 {sid} 不存在於房屋設定")
            for day_type in DAY_TYPES:
                if m.routines.get(day_type) not in self.routines:
                    raise ValueError(f"{m.name} 的{day_type}作息 {m.routines.get(day_type)} 不存在")
                # 實際算一次，順便檢查每個區塊的時間、空間、活動是否正確
                self.member_timeline(m, day_type)

    def _resolve_space(self, ref: str | None, member: Member) -> str | None:
        if ref is None:
            return None
        if ref == "@bedroom":
            return member.bedroom
        if ref == "@bathroom":
            return member.bathroom
        if ref not in {s.id for s in self.house.spaces}:
            raise ValueError(f"空間 {ref} 不存在於房屋設定")
        return ref

    def _apply(self, timeline: list[Slot], block: dict, member: Member) -> None:
        start, end = _to_minute(block["start"]), _to_minute(block["end"])
        if start >= end:
            raise ValueError(f"{member.name} 的區塊 {block['start']}–{block['end']} 開始時間需早於結束時間")
        if block["activity"] not in self.activities:
            raise ValueError(f"未定義的活動：{block['activity']}")
        slot = Slot(self._resolve_space(block["space"], member), block["activity"])
        timeline[start:end] = [slot] * (end - start)

    # ---------- 計算 ----------

    def member_timeline(self, member: Member, day_type: str) -> list[Slot]:
        """回傳長度 1440 的列表：該成員每分鐘的位置與活動。"""
        key = ("timeline", member.id, day_type)
        if key not in self._cache:
            routine = self.routines[member.routines[day_type]]
            default = routine["default"]
            timeline = [Slot(self._resolve_space(default["space"], member), default["activity"])] * MINUTES_PER_DAY
            for block in routine["blocks"] + member.extra_blocks.get(day_type, []):
                self._apply(timeline, block, member)
            self._cache[key] = timeline
        return self._cache[key]

    def activity_log(self, day_type: str) -> pd.DataFrame:
        """長表格：每小時、每位成員、每個(空間, 活動)停留的比例（0–1）。
        之後的負載模型可用它判斷「誰在哪裡做什麼」，例如煮飯時開排油煙機。"""
        key = ("log", day_type)
        if key not in self._cache:
            self._cache[key] = self._build_activity_log(day_type)
        return self._cache[key].copy()

    def _build_activity_log(self, day_type: str) -> pd.DataFrame:
        rows = []
        for m in self.members:
            timeline = self.member_timeline(m, day_type)
            for hour in range(24):
                minutes = timeline[hour * 60:(hour + 1) * 60]
                counts: dict[tuple, int] = {}
                for s in minutes:
                    counts[(s.space, s.activity)] = counts.get((s.space, s.activity), 0) + 1
                for (space, activity), n in counts.items():
                    rows.append({"hour": hour, "member": m.id, "role": m.role, "space": space,
                                 "activity": activity, "fraction": n / 60})
        return pd.DataFrame(rows)

    def presence(self, day_type: str, space_id: str, activities: set[str] | None = None) -> list[float]:
        """每小時中「至少有一人在該空間（且正在做指定活動）」的時間比例（0–1）。
        和 occupancy 不同：兩人同時看電視，電視只開一台，所以用「有沒有人」而不是「幾個人」。"""
        key = ("presence", day_type, space_id, frozenset(activities) if activities is not None else None)
        if key not in self._cache:
            minutes_used = [False] * MINUTES_PER_DAY
            for m in self.members:
                for i, s in enumerate(self.member_timeline(m, day_type)):
                    if s.space == space_id and (activities is None or s.activity in activities):
                        minutes_used[i] = True
            self._cache[key] = [sum(minutes_used[h * 60:(h + 1) * 60]) / 60 for h in range(24)]
        return list(self._cache[key])

    def occupancy(self, day_type: str, activity: str | None = None) -> pd.DataFrame:
        """24 列 × 各空間：每小時平均人數。可指定 activity（例如 "sleep" 只算睡覺的人）。"""
        log = self.activity_log(day_type)
        log = log[log["space"].notna()]
        if activity is not None:
            log = log[log["activity"] == activity]
        table = log.pivot_table(index="hour", columns="space", values="fraction",
                                aggfunc="sum", fill_value=0.0)
        # 依房屋空間順序排列，沒人使用的空間（例如公共設備）補 0
        return table.reindex(index=range(24), columns=[s.id for s in self.house.spaces], fill_value=0.0)

    def people_at_home(self, day_type: str) -> pd.Series:
        return self.occupancy(day_type).sum(axis=1)


def day_type_for(day: date, is_off_peak_day) -> str:
    """週末與國定假日（以台電離峰日判斷）視為假日作息。"""
    return "holiday" if is_off_peak_day(day) else "weekday"


if __name__ == "__main__":
    # 用法：python -m core.schedule  → 印出平日與假日的「24 小時 × 各空間」使用人數表
    from core.models import load_house

    pd.set_option("display.unicode.east_asian_width", True)
    pd.set_option("display.width", 160)

    house = load_house()
    schedule = Schedule(house)
    names = {s.id: s.name for s in house.spaces}
    for day_type, title in (("weekday", "平日"), ("holiday", "假日")):
        table = schedule.occupancy(day_type).rename(columns=names)
        table["在家人數"] = table.sum(axis=1)
        table.index = [f"{h:02d}:00" for h in range(24)]
        print(f"\n=== {title}：各空間每小時平均使用人數（人）===")
        print(table.round(2).to_string())
        person_hours = table.drop(columns="在家人數").sum()
        print("全日人·時：" + "、".join(f"{k} {v:.1f}" for k, v in person_hours.items()))
