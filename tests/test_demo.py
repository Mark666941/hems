"""E2：示範帳號與示範資料。"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core.storage import Store
from ui.demo import DEMO_USER, create_demo_profiles, demo_profiles, enter_demo
from ui.settings import build_analyzer

APP = str(Path(__file__).resolve().parent.parent / "app.py")
UI = {"plan_id": "tou_2", "onboarded": False}


@pytest.fixture
def store(tmp_path):
    return Store(f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}")


def test_demo_profiles_are_valid():
    profiles = demo_profiles(UI)
    assert [p[0] for p in profiles] == ["示範：四口之家（標配）", "示範：小家庭（瓦斯熱水器）"]
    for _, settings, ui, records in profiles:
        build_analyzer(settings)                                  # 設定有效
        assert ui["onboarded"] is True                            # 不再顯示新手引導提示
        assert {k for k, _ in records} >= {"habit", "bill"}
    small = profiles[1][1]
    assert sum(s["template"] == "bedroom" for s in small["house"]["spaces"]) == 2
    assert profiles[1][2]["plan_id"] == "progressive"


def test_enter_demo_resets_every_time(store):
    uid = enter_demo(store, UI)
    profiles = store.list_profiles(uid)
    assert len(profiles) == 2
    first = profiles[0][0]
    assert store.last_profile(uid) == first                       # 進入後看到四口之家
    assert len(store.list_records(uid, first, "bill")) == 2
    store.rename_profile(uid, first, "被訪客改掉的名字")
    store.add_record(uid, first, "habit", {"space_id": "x", "appliance_id": "y", "weekdays": [0], "start": 1, "end": 2})
    uid2 = enter_demo(store, UI)
    assert uid2 == uid
    names = [n for _, n in store.list_profiles(uid)]
    assert names == ["示範：四口之家（標配）", "示範：小家庭（瓦斯熱水器）"]   # 前一位訪客的修改不會留下
    with store.engine.connect() as conn:
        total = conn.exec_driver_sql("SELECT COUNT(*) FROM usage_records").scalar()
    assert total == sum(len(p[3]) for p in demo_profiles(UI))     # 舊紀錄已清除


def test_demo_user_cannot_login_with_password(store):
    enter_demo(store, UI)
    assert store.login(DEMO_USER, "") is None
    assert store.login(DEMO_USER, "anything") is None


def test_create_demo_profiles_keeps_existing(store):
    uid = store.register("mark", "secret123")
    mine = store.create_profile(uid, "我家", {"x": 1}, {})
    create_demo_profiles(store, uid, UI)
    ids = [pid for pid, _ in store.list_profiles(uid)]
    assert mine in ids and len(ids) == 3


def test_login_screen_demo_button(monkeypatch):
    monkeypatch.setenv("HEMS_REQUIRE_LOGIN", "1")
    at = AppTest.from_file(APP, default_timeout=180)
    at.session_state["_test_page"] = "forecast"
    at.run()
    assert "user_id" not in at.session_state
    at.button(key="demo_login").click().run()
    assert not at.exception and not at.error
    assert at.session_state.profile_name == "示範：四口之家（標配）"
    md = " ".join(m.value for m in at.markdown)
    assert "帳單合計比模擬" in md                                    # 預測頁已有示範帳單


def test_warmup_key_matches_demo_profile(tmp_path):
    # app.py 的背景預先計算用 migrate(default_settings()) 的快取 key；
    # 訪客按「示範帳號」後，設定經資料庫存取、migrate 後的 key 必須完全相同，預先算好的結果才用得到
    from core.storage import Store
    from ui.account import migrate
    from ui.demo import enter_demo
    from ui.settings import default_settings, sim_key
    store = Store(f"sqlite:///{(tmp_path / 'w.db').as_posix()}")
    uid = enter_demo(store, {"onboarded": False})
    loaded = migrate(store.load_profile(uid, store.last_profile(uid)).settings)
    assert sim_key(loaded) == sim_key(migrate(default_settings()))
