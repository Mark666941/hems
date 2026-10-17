"""資料結構：家電、空間、房屋，以及從 JSON 設定檔組裝房屋的函式。

設定分三層：
    家電目錄（appliances.json）→ 空間範本（space_templates.json）→ 房屋（default_house.json）
下層只需寫「和上層不同的地方」，標配房型因此不會寫死在程式裡。

家庭成員（使用者）在下方 Member；作息邏輯在 schedule.py；模式會在階段 6 加入。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

import pandas as pd

from core.solar import ORIENTATIONS

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# 房屋設定中可以覆寫的家電欄位
_OVERRIDABLE_FIELDS = {"name", "power_w", "enabled", "capacity_kw", "grade"}


class LoadType(Enum):
    """家電負載類型，決定最佳化時能怎麼調整。"""

    ESSENTIAL = "essential"    # 必要負載：任何模式都不關閉
    ADJUSTABLE = "adjustable"  # 可調負載：可調整設定或開關
    SHIFTABLE = "shiftable"    # 可移轉負載：可移到離峰時段

    @property
    def label(self) -> str:
        return {"essential": "必要", "adjustable": "可調", "shiftable": "可移轉"}[self.value]


@dataclass
class Appliance:
    """一台家電。"""

    id: str
    name: str
    power_w: float
    load_type: LoadType
    enabled: bool = True
    capacity_kw: float | None = None   # 冷氣額定冷房能力（kW）；None 時由額定耗電 × 額定 COP 推算
    grade: int | None = None           # 冷氣能源效率等級 1–5；None 時用 load_params.json 的 rated_cop


@dataclass
class Space:
    """一個空間（由範本建立的實例）。"""

    id: str
    name: str
    template: str
    area_ping: float
    appliances: list[Appliance] = field(default_factory=list)
    orientation: str | None = None   # 窗戶與外牆朝向：N/NE/E/SE/S/SW/W/NW；None 代表未指定（取四面平均）

    @property
    def total_power_w(self) -> float:
        """所有啟用家電同時運轉的功率總和（W）。"""
        return sum(a.power_w for a in self.appliances if a.enabled)

    def get_appliance(self, appliance_id: str) -> Appliance | None:
        return next((a for a in self.appliances if a.id == appliance_id), None)


@dataclass
class House:
    """一間房屋 = 多個空間的組合。"""

    name: str
    occupants: int
    spaces: list[Space]

    @property
    def total_area_ping(self) -> float:
        return sum(s.area_ping for s in self.spaces)

    def get_space(self, space_id: str) -> Space:
        for s in self.spaces:
            if s.id == space_id:
                return s
        raise KeyError(f"找不到空間：{space_id}")

    def all_appliances(self) -> list[tuple[Space, Appliance]]:
        """列出所有 (空間, 家電) 配對。"""
        return [(s, a) for s in self.spaces for a in s.appliances]

    def to_dataframe(self) -> pd.DataFrame:
        """整理成表格：一列一台家電，方便列印與之後在 Streamlit 顯示。"""
        rows = [
            {
                "空間": s.name,
                "範本": s.template,
                "面積(坪)": s.area_ping,
                "家電": a.name,
                "功率(W)": a.power_w,
                "負載類型": a.load_type.label,
                "啟用": a.enabled,
            }
            for s, a in self.all_appliances()
        ]
        return pd.DataFrame(rows)


@dataclass
class Member:
    """家庭成員：自己的臥室、浴室，以及平日／假日各套用哪個作息。"""

    id: str
    name: str
    role: str                      # adult / child，之後可用於代謝率等差異
    bedroom: str                   # 空間 id
    bathroom: str                  # 空間 id
    routines: dict[str, str]       # {"weekday": 作息 id, "holiday": 作息 id}
    extra_blocks: dict[str, list[dict]] = field(default_factory=dict)


# ---------- 讀取設定檔 ----------

def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # 以底線開頭的鍵是說明文字，不是設定
    return {k: v for k, v in data.items() if not k.startswith("_")}


def load_appliance_catalog(path: Path = CONFIG_DIR / "appliances.json") -> dict[str, Appliance]:
    """讀取家電目錄，回傳 {家電 id: 預設家電}。"""
    return {
        aid: Appliance(
            id=aid,
            name=spec["name"],
            power_w=float(spec["power_w"]),
            load_type=LoadType(spec["load_type"]),
        )
        for aid, spec in _load_json(path).items()
    }


def load_space_templates(path: Path = CONFIG_DIR / "space_templates.json") -> dict[str, dict]:
    return _load_json(path)


def _make_appliance(entry: str | dict, catalog: dict[str, Appliance]) -> Appliance:
    """依目錄建立家電；entry 可為字串 id，或含 id 與覆寫欄位的物件。"""
    if isinstance(entry, str):
        entry = {"id": entry}
    aid = entry["id"]
    if aid not in catalog:
        raise ValueError(f"家電目錄中沒有：{aid}")
    return _apply_overrides(catalog[aid], {k: v for k, v in entry.items() if k != "id"})


def _apply_overrides(appliance: Appliance, overrides: dict) -> Appliance:
    unknown = set(overrides) - _OVERRIDABLE_FIELDS
    if unknown:
        raise ValueError(f"家電 {appliance.id} 有不能覆寫的欄位：{unknown}")
    # replace 會產生新物件，避免不同空間共用同一台家電
    return replace(appliance, **overrides)


def build_space(entry: dict, templates: dict[str, dict], catalog: dict[str, Appliance]) -> list[Space]:
    """依房屋設定的一筆空間資料建立空間；count > 1 時建立多個相同空間。"""
    template_id = entry["template"]
    if template_id not in templates:
        raise ValueError(f"沒有這個空間範本：{template_id}")
    template = templates[template_id]

    # 1. 從範本取得預設家電，2. 移除、3. 新增、4. 覆寫參數
    removed = set(entry.get("remove_appliances", []))
    entries = [e for e in template["appliances"]
               if (e if isinstance(e, str) else e["id"]) not in removed]
    entries += entry.get("add_appliances", [])
    overrides = entry.get("appliance_overrides", {})

    orientation = entry.get("orientation") or None
    if orientation is not None and orientation not in ORIENTATIONS:
        raise ValueError(f"窗戶方位必須是 {'、'.join(ORIENTATIONS)} 之一：{orientation}")
    count = entry.get("count", 1)
    spaces = []
    for i in range(1, count + 1):
        appliances = []
        for e in entries:
            a = _make_appliance(e, catalog)
            if a.id in overrides:
                a = _apply_overrides(a, overrides[a.id])
            appliances.append(a)
        suffix = f"_{i}" if count > 1 else ""
        name_suffix = f" {i}" if count > 1 else ""
        spaces.append(Space(
            id=entry["id"] + suffix,
            name=entry.get("name", template["name"]) + name_suffix,
            template=template_id,
            area_ping=float(entry.get("area_ping", template["default_area_ping"])),
            appliances=appliances,
            orientation=orientation,
        ))
    return spaces


def load_house(
    house_path: Path = CONFIG_DIR / "default_house.json",
    templates_path: Path = CONFIG_DIR / "space_templates.json",
    catalog_path: Path = CONFIG_DIR / "appliances.json",
) -> House:
    """讀取三個設定檔並組裝成房屋。"""
    return house_from_config(_load_json(house_path), load_space_templates(templates_path),
                             load_appliance_catalog(catalog_path))


def house_from_config(config: dict, templates: dict[str, dict], catalog: dict[str, Appliance]) -> House:
    """由設定字典組裝房屋（介面讓使用者客製化房型時，不必寫回檔案）。"""
    spaces = [s for entry in config["spaces"] for s in build_space(entry, templates, catalog)]
    ids = [s.id for s in spaces]
    if len(ids) != len(set(ids)):
        raise ValueError("空間 id 重複")
    return House(name=config["name"], occupants=config["occupants"], spaces=spaces)


if __name__ == "__main__":
    # 執行 python -m core.models 列出標配房型的所有空間與家電
    pd.set_option("display.unicode.east_asian_width", True)
    pd.set_option("display.width", 120)

    house = load_house()
    print(f"{house.name}｜{house.occupants} 人｜總面積 {house.total_area_ping} 坪\n")
    print(house.to_dataframe().to_string(index=False))
    print("\n各空間家電功率合計（W）：")
    for s in house.spaces:
        print(f"  {s.name:<6}{s.total_power_w:>8.0f}")
