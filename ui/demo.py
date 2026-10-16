"""示範帳號與示範資料（E2）：讓第一次看的人（例如面試官）不用註冊、不用設定，就能看到完整功能。

- 網頁版：登入畫面的「以示範帳號試用」→ 進入「示範帳號」（沒有密碼、不能用帳號密碼登入），
  每次進入都重設成下面的示範資料，前一位訪客的修改不會留下來。
- 桌面版：「帳號與設定檔」視窗的「載入示範設定檔」→ 在目前使用者下新增示範設定檔（不刪除原有的）。

示範設定檔：
1. 四口之家（標配房型）：已填好每週習慣（電視、電腦）、一筆每日紀錄、兩期台電帳單 → 預測頁有完整內容
2. 小家庭（兩房一衛、三人、瓦斯熱水器、累進電價）：用新手引導的規則產生，示範不同家庭的差異
"""
from __future__ import annotations

from core.storage import Store
from ui.account import ui_to_json
from ui.settings import default_settings
from ui.wizard import apply_wizard, default_draft

DEMO_USER = "示範帳號"
WEEKDAYS, WEEKEND = [0, 1, 2, 3, 4], [5, 6]

FAMILY_RECORDS = [
    ("habit", {"space_id": "living_room", "appliance_id": "tv", "weekdays": WEEKDAYS, "start": 19, "end": 23}),
    ("habit", {"space_id": "living_room", "appliance_id": "tv", "weekdays": WEEKEND, "start": 14, "end": 23}),
    ("habit", {"space_id": "bedroom_1", "appliance_id": "computer", "weekdays": WEEKDAYS, "start": 20, "end": 23}),
    ("daily", {"date": "2026-07-18", "space_id": "living_room", "appliance_id": "tv", "hours": 9}),
    ("bill", {"start": "2026-05-01", "end": "2026-06-30", "kwh": 1090, "amount": 3050}),
    ("bill", {"start": "2026-07-01", "end": "2026-08-31", "kwh": 1210, "amount": 3720}),
]
SMALL_RECORDS = [
    ("habit", {"space_id": "living_room", "appliance_id": "tv", "weekdays": list(range(7)), "start": 20, "end": 22}),
    ("bill", {"start": "2026-07-01", "end": "2026-08-31", "kwh": 760, "amount": 2290}),
]


def demo_profiles(default_ui: dict) -> list[tuple[str, dict, dict, list]]:
    """[(名稱, 設定, 介面選項, 紀錄)]。"""
    ui = {**default_ui, "onboarded": True}
    family = default_settings()
    draft = {**default_draft(family, ui), "bedrooms": 2, "bathrooms": 1, "bedroom_ping": [5.0, 3.5],
             "living_ping": 6.0, "kitchen_ping": 2.5, "adults": 2, "children": 1, "water_heater": "gas",
             "plan_id": "progressive"}
    small, small_ui = apply_wizard(default_settings(), ui, draft)
    return [("示範：四口之家（標配）", family, ui, FAMILY_RECORDS),
            ("示範：小家庭（瓦斯熱水器）", small, small_ui, SMALL_RECORDS)]


def create_demo_profiles(store: Store, user_id: int, default_ui: dict) -> int:
    """在使用者底下建立示範設定檔與紀錄，回傳第一個示範設定檔的 id（並設為最後使用）。"""
    ids = []
    for name, settings, ui, records in demo_profiles(default_ui):
        pid = store.create_profile(user_id, name, settings, ui_to_json(ui))
        for kind, data in records:
            store.add_record(user_id, pid, kind, data)
        ids.append((pid, settings, ui))
    first, settings, ui = ids[0]
    store.save_profile(user_id, first, settings, ui_to_json(ui))      # 設為最後使用的設定檔
    return first


def enter_demo(store: Store, default_ui: dict) -> int:
    """進入示範帳號：重設成示範資料，回傳使用者 id。"""
    user_id = store.passwordless_user(DEMO_USER)
    store.reset_user(user_id)
    create_demo_profiles(store, user_id, default_ui)
    return user_id
