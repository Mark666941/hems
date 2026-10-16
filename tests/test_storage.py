"""第 3 批：資料庫儲存（帳號、設定檔、自動存檔、匯出／匯入）。"""
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core.storage import Store, export_profile, hash_password, parse_export
from ui.account import ui_from_json, ui_to_json
from ui.settings import default_settings


@pytest.fixture
def store(tmp_path):
    return Store(f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}")


# ---------- 帳號 ----------

def test_register_and_login(store):
    uid = store.register("mark", "secret123")
    assert store.login("mark", "secret123") == uid
    assert store.login("mark", "wrong-password") is None
    assert store.login("nobody", "secret123") is None


def test_password_is_hashed_not_stored(store):
    store.register("mark", "secret123")
    with store.engine.connect() as conn:
        row = conn.exec_driver_sql("SELECT password_hash, salt FROM users WHERE username='mark'").one()
    assert "secret123" not in row.password_hash
    assert row.password_hash == hash_password("secret123", row.salt)
    assert len(row.salt) == 32          # 16 bytes 隨機鹽


@pytest.mark.parametrize("username, password, message", [
    ("ab", "secret123", "帳號長度"), ("mark", "123", "密碼至少"),
])
def test_register_validation(store, username, password, message):
    with pytest.raises(ValueError, match=message):
        store.register(username, password)


def test_duplicate_username(store):
    store.register("mark", "secret123")
    with pytest.raises(ValueError, match="已經有人使用"):
        store.register("mark", "another123")


def test_local_user_cannot_login_with_password(store):
    uid = store.local_user()
    assert store.local_user() == uid               # 只會建立一次
    assert store.login("本機使用者", "") is None    # 沒有密碼，不能從網頁登入


# ---------- 設定檔 ----------

def test_profile_crud_and_last_used(store):
    uid = store.local_user()
    assert store.last_profile(uid) is None
    a = store.create_profile(uid, "我家", default_settings(), {"hour": 9})
    b = store.create_profile(uid, "爸媽家", default_settings(), {"hour": 20})
    assert store.list_profiles(uid) == [(a, "我家"), (b, "爸媽家")]
    assert store.last_profile(uid) == b                     # 最後建立／使用的

    settings = default_settings()
    settings["baseline_setpoint_c"] = 27.0
    store.save_profile(uid, a, settings, {"hour": 15})
    loaded = store.load_profile(uid, a)
    assert loaded.settings["baseline_setpoint_c"] == 27.0 and loaded.ui == {"hour": 15}
    assert store.last_profile(uid) == a                     # 存檔後變成最後使用

    store.rename_profile(uid, b, "老家")
    store.delete_profile(uid, b)
    assert store.list_profiles(uid) == [(a, "我家")]
    with pytest.raises(ValueError, match="至少要保留"):
        store.delete_profile(uid, a)


def test_users_cannot_access_each_other(store):
    alice, bob = store.register("alice", "secret123"), store.register("bobby", "secret123")
    pid = store.create_profile(alice, "Alice 家", default_settings(), {})
    with pytest.raises(KeyError):
        store.load_profile(bob, pid)
    with pytest.raises(KeyError):
        store.save_profile(bob, pid, default_settings(), {})
    assert store.list_profiles(bob) == []


# ---------- 匯出／匯入 ----------

def test_export_import_round_trip(store):
    uid = store.local_user()
    pid = store.create_profile(uid, "我家", default_settings(), {"custom_day": "2026-07-20"})
    name, settings, ui = parse_export(export_profile(store.load_profile(uid, pid)))
    assert name == "我家" and settings == default_settings() and ui == {"custom_day": "2026-07-20"}


@pytest.mark.parametrize("text, message", [
    ("not json", "不是有效的 JSON"), ('{"a": 1}', "不是 HEMS"),
    ('{"hems_profile_version": 1, "settings": {"house": {}}}', "不完整"),
])
def test_import_rejects_bad_files(text, message):
    with pytest.raises(ValueError, match=message):
        parse_export(text)


def test_ui_json_round_trip():
    defaults = {"custom_day": date(2026, 7, 20), "hour": 9, "plan_id": "tou_2"}
    ui = {"custom_day": date(2026, 4, 15), "hour": 21, "plan_id": "progressive"}
    restored = ui_from_json(ui_to_json(ui) | {"unknown": 1}, defaults)
    assert restored == ui                                   # 日期還原為 date、未知欄位忽略


# ---------- 介面：自動存檔，重新開啟後設定仍在 ----------

def _app() -> AppTest:
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=180)
    at.run()
    return at


def test_settings_survive_restart():
    first = _app()
    assert not first.exception
    first.session_state.settings["baseline_setpoint_c"] = 24.5
    first.session_state.ui["hour"] = 5
    first.run()                       # 這次執行結束時自動存檔
    assert not first.exception

    second = _app()                   # 模擬關掉程式後重新打開（新的工作階段）
    try:
        assert second.session_state.settings["baseline_setpoint_c"] == 24.5
        assert second.session_state.ui["hour"] == 5
        assert any("自動存檔" in c.value for c in second.sidebar.caption)
    finally:                          # 還原預設值，避免影響其他測試
        second.session_state.settings = default_settings()
        second.run()


def test_login_screen_register_and_login(monkeypatch):
    """網頁版流程：未登入只看到登入畫面；註冊後進入首頁；錯誤密碼不能登入。"""
    monkeypatch.setenv("HEMS_REQUIRE_LOGIN", "1")
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=180)
    at.run()
    assert not at.exception
    assert "user_id" not in at.session_state
    assert not any("此刻用電" in m.value for m in at.markdown)          # 還沒登入，看不到首頁

    username = f"user{__import__('secrets').token_hex(3)}"
    at.text_input[2].input(username)      # 註冊分頁：帳號、密碼、確認密碼
    at.text_input[3].input("secret123")
    at.text_input[4].input("secret123")
    at.button[1].click().run()            # 「註冊並登入」
    assert not at.exception
    assert "user_id" in at.session_state
    assert any("此刻用電" in m.value for m in at.markdown)              # 登入後看到首頁

    other = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=180)
    other.run()
    other.text_input[0].input(username)
    other.text_input[1].input("wrong-password")
    other.button[0].click().run()
    assert "user_id" not in other.session_state
    assert any("帳號或密碼錯誤" in e.value for e in other.error)


def test_old_default_names_are_migrated():
    from ui.account import migrate
    s = default_settings()
    names = ["家長 1", "家長 2", "小孩 1", "自訂名字"]
    for m, n in zip(s["schedule"]["members"], names):
        m["name"] = n
    assert [m["name"] for m in migrate(s)["schedule"]["members"]] == ["爸爸", "媽媽", "哥哥", "自訂名字"]
