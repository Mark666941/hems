"""帳號、設定檔與自動存檔（介面端）。

流程（每次執行 app.py）：
1. ensure_session()：桌面版（SQLite）自動以「本機使用者」登入；網頁版（PostgreSQL）未登入就顯示登入畫面。
   登入後載入最後使用的設定檔（沒有就用預設值建立「我家」）。
2. 使用者照常操作，設定存在 session_state。
3. autosave()：每次執行結束前，若設定或介面選項有變動就寫回資料庫（不用按儲存）。
"""
from __future__ import annotations

import copy
import json
import os
from datetime import date

import streamlit as st

from core.storage import Store, export_profile, parse_export
from ui.settings import build_analyzer, default_settings


@st.cache_resource(show_spinner=False)
def get_store() -> Store:
    """資料庫連線（整個程式共用一個）。網址：環境變數 > Streamlit secrets > 本機 SQLite。"""
    url = os.environ.get("HEMS_DATABASE_URL")
    if not url:
        try:
            url = st.secrets["database"]["url"]
        except Exception:   # 沒有 secrets 檔或沒有設定 database.url
            url = None
    return Store(url)


def login_required(store: Store) -> bool:
    """網頁版（PostgreSQL）一定要登入；桌面版可設環境變數 HEMS_REQUIRE_LOGIN=1 強制登入（測試、展示用）。"""
    return not store.is_local or os.environ.get("HEMS_REQUIRE_LOGIN") == "1"


# ---------- 介面選項 ↔ JSON ----------

def ui_to_json(ui: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in ui.items()}


def ui_from_json(data: dict, defaults: dict) -> dict:
    ui = dict(defaults)
    for k, v in data.items():
        if k in defaults:
            ui[k] = date.fromisoformat(v) if isinstance(defaults[k], date) and isinstance(v, str) else v
    return ui


def _snapshot() -> str:
    return json.dumps({"settings": st.session_state.settings, "ui": ui_to_json(st.session_state.ui)},
                      ensure_ascii=False, sort_keys=True)


def _activate(store: Store, user_id: int, profile_id: int, default_ui: dict) -> None:
    profile = store.load_profile(user_id, profile_id)
    st.session_state.update(
        user_id=user_id, profile_id=profile.id, profile_name=profile.name,
        settings=profile.settings, ui=ui_from_json(profile.ui, default_ui), remote={}, remote_day=None)
    st.session_state.saved_snapshot = _snapshot()


# ---------- 登入與載入設定檔 ----------

def ensure_session(default_ui: dict) -> Store:
    """確保已登入並載入設定檔；網頁版未登入時顯示登入畫面並停止這次執行。"""
    store = get_store()
    if "user_id" not in st.session_state:
        if login_required(store):
            _login_screen(store)
            st.stop()
        st.session_state.user_id = store.local_user()
    if "profile_id" not in st.session_state:
        user_id = st.session_state.user_id
        profile_id = store.last_profile(user_id)
        if profile_id is None:
            profile_id = store.create_profile(user_id, "我家", default_settings(), ui_to_json(default_ui))
        _activate(store, user_id, profile_id, default_ui)
    return store


def _login_screen(store: Store) -> None:
    st.markdown('<div class="hems-head"><div class="title"><small>HEMS · 居家能源管理</small>登入</div></div>',
                unsafe_allow_html=True)
    left, _ = st.columns([2, 3])
    with left:
        tab_login, tab_register = st.tabs(["登入", "註冊新帳號"])
        with tab_login, st.form("login"):
            username = st.text_input("帳號")
            password = st.text_input("密碼", type="password")
            if st.form_submit_button("登入", type="primary", width="stretch"):
                user_id = store.login(username, password)
                if user_id is None:
                    st.error("帳號或密碼錯誤")
                else:
                    st.session_state.user_id = user_id
                    st.rerun()
        with tab_register, st.form("register"):
            username = st.text_input("帳號（3–32 字）")
            password = st.text_input("密碼（至少 6 字）", type="password")
            confirm = st.text_input("再輸入一次密碼", type="password")
            if st.form_submit_button("註冊並登入", type="primary", width="stretch"):
                if password != confirm:
                    st.error("兩次輸入的密碼不一樣")
                else:
                    try:
                        st.session_state.user_id = store.register(username, password)
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
        st.caption("密碼以 PBKDF2-SHA256 加鹽雜湊後儲存，系統不會保存明碼。")


# ---------- 自動存檔 ----------

def autosave(store: Store) -> bool:
    """設定或介面選項有變動就寫回資料庫。回傳是否有存檔。"""
    if "profile_id" not in st.session_state:
        return False
    snapshot = _snapshot()
    if snapshot == st.session_state.get("saved_snapshot"):
        return False
    store.save_profile(st.session_state.user_id, st.session_state.profile_id,
                       st.session_state.settings, ui_to_json(st.session_state.ui))
    st.session_state.saved_snapshot = snapshot
    return True


# ---------- 帳號與設定檔視窗 ----------

def _valid(settings: dict) -> str | None:
    """匯入前先試著建立系統，有錯回傳錯誤訊息。"""
    try:
        build_analyzer(settings)
        return None
    except (ValueError, KeyError, TypeError) as e:
        return str(e)


@st.dialog("👤 帳號與設定檔", width="large")
def account_dialog(default_ui: dict) -> None:
    store = get_store()
    user_id = st.session_state.user_id
    autosave(store)                       # 切換前先存好目前的修改
    st.caption(f"使用者：{store.username(user_id)}｜"
               + ("本機資料庫（桌面版，自動存檔）" if store.is_local else "雲端資料庫（自動存檔）"))

    profiles = dict(store.list_profiles(user_id))
    current = st.session_state.profile_id
    c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
    choice = c1.selectbox("設定檔", list(profiles), index=list(profiles).index(current),
                          format_func=lambda pid: profiles[pid] + ("（使用中）" if pid == current else ""))
    if c2.button("切換", type="primary", width="stretch", disabled=choice == current):
        _activate(store, user_id, choice, default_ui)
        st.session_state.flash = f"已切換到「{profiles[choice]}」"
        st.rerun()
    c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
    new_name = c1.text_input("重新命名", value=profiles[choice], key=f"rename_{choice}")
    if c2.button("重新命名", width="stretch", disabled=new_name.strip() in ("", profiles[choice])):
        store.rename_profile(user_id, choice, new_name)
        if choice == current:
            st.session_state.profile_name = new_name.strip()
        st.rerun()
    if st.button("🗑️ 刪除這個設定檔", disabled=len(profiles) <= 1 or choice == current,
                 help="不能刪除使用中的設定檔，也至少要保留一個"):
        store.delete_profile(user_id, choice)
        st.rerun()

    st.divider()
    st.markdown("**新增設定檔**")
    c1, c2 = st.columns([2, 1])
    name = c1.text_input("名稱", placeholder="例如：爸媽家")
    source = c2.radio("內容", ["複製目前設定", "標配房型預設值"], label_visibility="collapsed")
    if st.button("建立並切換", type="primary", disabled=not name.strip()):
        settings = copy.deepcopy(st.session_state.settings) if source == "複製目前設定" else default_settings()
        ui = ui_to_json(st.session_state.ui if source == "複製目前設定" else default_ui)
        profile_id = store.create_profile(user_id, name, settings, ui)
        _activate(store, user_id, profile_id, default_ui)
        st.session_state.flash = f"已建立並切換到「{name.strip()}」"
        st.rerun()

    st.divider()
    st.markdown("**匯出／匯入**（JSON 檔，可備份或帶到其他電腦）")
    c1, c2 = st.columns(2)
    profile = store.load_profile(user_id, current)
    c1.download_button("⬇️ 匯出目前設定檔", export_profile(profile).encode("utf-8"),
                       f"hems_{profile.name}.json", "application/json", width="stretch")
    uploaded = c2.file_uploader("匯入設定檔", type="json", label_visibility="collapsed")
    if uploaded is not None:
        try:
            name, settings, ui = parse_export(uploaded.getvalue().decode("utf-8"))
            error = _valid(settings)
            if error:
                st.error(f"設定內容有誤，無法匯入：{error}")
            elif st.button(f"匯入為新設定檔「{name}」", type="primary"):
                profile_id = store.create_profile(user_id, name, settings, ui)
                _activate(store, user_id, profile_id, default_ui)
                st.session_state.flash = f"已匯入「{name}」"
                st.rerun()
        except ValueError as e:
            st.error(str(e))

    if login_required(store):
        st.divider()
        if st.button("登出"):
            for k in ("user_id", "profile_id", "profile_name", "settings", "ui", "saved_snapshot"):
                st.session_state.pop(k, None)
            st.rerun()
