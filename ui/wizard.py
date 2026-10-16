"""🧭 新手引導精靈（C1）：四個步驟完成最重要的設定——房型、家庭成員與作息、電價方案、熱水器。

- default_draft()：從目前設定讀出精靈的初始值（每一步的選項都先帶入目前的設定）
- apply_wizard()：把精靈的答案套用到設定（純函式，方便測試）
- wizard_dialog()：介面（st.dialog）。各步驟的答案先存在 session_state.wizard，最後按「完成」才一次套用。
  open_wizard() 只設定「開啟中」旗標，由 app.py 的 show_wizard_if_open() 顯示；按 × 關閉時清除旗標。
"""
from __future__ import annotations

import streamlit as st

from core.tariff import Tariff
from ui.family import add_member
from ui.settings import (
    apply_routine_times,
    apply_spaces_table,
    build_house,
    routine_times,
    spaces_table,
)

STEPS = ["房型", "家庭成員與作息", "電價方案", "熱水器"]
DEFAULT_BEDROOM_PING = 3.5
DEFAULT_BATHROOM_PING = 1.5
PLAN_TIPS = {
    "progressive": "不分時段、用越多每度越貴。晚上才在家、無法把用電移到半夜的家庭適合。",
    "tou_2": "分尖峰／離峰。能把熱水器、洗衣、烘衣移到半夜或週末的家庭適合。",
    "tou_3": "傍晚 16–22 時最貴。傍晚到晚上用電很少的家庭才划算。",
}


def _water_heater_key(settings: dict) -> str | None:
    for s, a in build_house(settings).all_appliances():
        if a.id == "water_heater":
            return f"{s.id}/{a.id}"
    return None


def default_draft(settings: dict, ui: dict) -> dict:
    """精靈的初始答案＝目前的設定。"""
    rows = spaces_table(settings)
    bedrooms = [r for r in rows if r["範本"] == "bedroom"]
    bathrooms = [r for r in rows if r["範本"] == "bathroom"]
    first = {t: next((r["坪數"] for r in rows if r["範本"] == t), 0.0) for t in ("living_room", "kitchen")}
    members = settings["schedule"]["members"]
    routines = settings["schedule"]["routines"]
    key = _water_heater_key(settings)
    house = build_house(settings)
    heater_on = False
    if key:
        sid, aid = key.split("/")
        heater_on = house.get_space(sid).get_appliance(aid).enabled
    return {
        "bedrooms": len(bedrooms), "bathrooms": max(1, len(bathrooms)),
        "bedroom_ping": [r["坪數"] for r in bedrooms], "living_ping": first["living_room"],
        "kitchen_ping": first["kitchen"],
        "adults": sum(1 for m in members if m["role"] == "adult"),
        "children": sum(1 for m in members if m["role"] == "child"),
        "adult_times": routine_times(routines["adult_weekday"]),
        "child_times": routine_times(routines["child_weekday"]),
        "plan_id": ui.get("plan_id", "tou_2"),
        "water_heater": "electric" if heater_on else "gas",
    }


def _resize(rows: list[dict], n: int, template: str, label: str, ping: float) -> list[dict]:
    """保留前 n 個同類空間（原有的家電設定不變），不足的依範本新增。"""
    rows = rows[:n]
    for k in range(len(rows), n):
        rows.append({"id": "", "名稱": f"{label} {k + 1}", "範本": template, "坪數": ping})
    return rows


def apply_wizard(settings: dict, ui: dict, draft: dict) -> tuple[dict, dict]:
    """把精靈的答案套用到設定，回傳（新設定, 新介面選項）。"""
    rows = spaces_table(settings)
    bedrooms = _resize([r for r in rows if r["範本"] == "bedroom"], int(draft["bedrooms"]), "bedroom", "臥室",
                       DEFAULT_BEDROOM_PING)
    for r, ping in zip(bedrooms, draft.get("bedroom_ping", [])):
        r["坪數"] = float(ping)
    bathrooms = _resize([r for r in rows if r["範本"] == "bathroom"], int(draft["bathrooms"]), "bathroom", "浴室",
                        DEFAULT_BATHROOM_PING)
    others = []
    for r in rows:
        if r["範本"] in ("bedroom", "bathroom"):
            continue
        if r["範本"] == "living_room" and not any(o["範本"] == "living_room" for o in others):
            r = {**r, "坪數": float(draft["living_ping"])}
        if r["範本"] == "kitchen" and not any(o["範本"] == "kitchen" for o in others):
            r = {**r, "坪數": float(draft["kitchen_ping"])}
        others.append(r)
    # 順序同標配房型：臥室、客廳與廚房、浴室、公共設備
    indoor = [o for o in others if o["範本"] != "utility"]
    utility = [o for o in others if o["範本"] == "utility"]
    new = apply_spaces_table(settings, bedrooms + indoor + bathrooms + utility)
    house = build_house(new)
    bedroom_ids = [s.id for s in house.spaces if s.template == "bedroom"]
    bathroom_ids = [s.id for s in house.spaces if s.template == "bathroom"]

    # 成員：保留原有的大人／小孩（名字不變），多的從後面刪除、不足的新增
    members = new["schedule"]["members"]
    kept = []
    for role, count in (("adult", int(draft["adults"])), ("child", int(draft["children"]))):
        kept += [m for m in members if m["role"] == role][:count]
    new["schedule"]["members"] = [m for m in members if m in kept]
    for role, count in (("adult", int(draft["adults"])), ("child", int(draft["children"]))):
        have = sum(1 for m in new["schedule"]["members"] if m["role"] == role)
        for _ in range(have, count):
            new, _msg = add_member(new, house, "", role, bedroom_ids[0])
    # 臥室與浴室：大人住第一間臥室（主臥）；小孩依序分配其他臥室（不夠時共用）
    children = 0
    for m in new["schedule"]["members"]:
        if m["role"] == "adult" or len(bedroom_ids) == 1:
            m["bedroom"], m["bathroom"] = bedroom_ids[0], bathroom_ids[0]
        else:
            m["bedroom"] = bedroom_ids[1 + children % (len(bedroom_ids) - 1)]
            m["bathroom"] = bathroom_ids[-1]
            children += 1
        m["routines"]["weekday"] = f"{m['role']}_weekday"   # 平日作息改用精靈設定的範本
    new = apply_routine_times(new, "adult_weekday", draft["adult_times"])
    new = apply_routine_times(new, "child_weekday", draft["child_times"])

    # 熱水器：電熱水器或瓦斯（瓦斯不計入用電）
    key = _water_heater_key(new)
    if key:
        edits = new.setdefault("appliance_edits", {})
        edits[key] = {**edits.get(key, {}), "enabled": draft["water_heater"] == "electric"}
    return new, {**ui, "plan_id": draft["plan_id"], "onboarded": True}


# ---------- 介面 ----------

def _hhmm(text: str | None, fallback: str) -> str:
    return text if text and text != "24:00" else fallback


def _go(delta: int) -> None:
    st.session_state.wizard_step = max(0, min(len(STEPS) - 1, st.session_state.wizard_step + delta))


def _save(**values) -> None:
    st.session_state.wizard.update(values)


def _time_options() -> list[str]:
    return [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]


def _times_editor(label: str, times: dict, key: str) -> dict:
    opts = _time_options()
    st.markdown(f"**{label}**")
    c = st.columns(4)
    out = {}
    for col, (k, name, fallback) in zip(c, [("wake", "起床", "07:00"), ("bed", "就寢", "23:00"),
                                            ("out_start", "出門", "08:00"), ("out_end", "回家", "18:00")]):
        value = _hhmm(times.get(k), fallback)
        out[k] = col.selectbox(name, opts, index=opts.index(value) if value in opts else 0, key=f"wz_{key}_{k}")
    return out


def open_wizard() -> None:
    """開啟精靈（app.py 每次執行時檢查 wizard_open，開啟中就顯示，切換步驟或重新整理都不會關掉）。"""
    st.session_state.wizard_open = True


def show_wizard_if_open() -> None:
    if st.session_state.get("wizard_open"):
        wizard_dialog()


def _dismiss() -> None:
    st.session_state.wizard_open = False


@st.dialog("🧭 新手引導", width="large", on_dismiss=_dismiss)
def wizard_dialog() -> None:
    if "wizard" not in st.session_state:
        st.session_state.wizard = default_draft(st.session_state.settings, st.session_state.ui)
        st.session_state.wizard_step = 0
    draft, step = st.session_state.wizard, st.session_state.wizard_step
    st.progress((step + 1) / len(STEPS), text=f"步驟 {step + 1}／{len(STEPS)}：{STEPS[step]}")

    if step == 0:
        st.caption("選擇房間數量與大小，冷房負載會依坪數計算。之後可以在平面圖或「設定」頁細調。")
        c1, c2 = st.columns(2)
        n_bed = c1.number_input("臥室", 1, 6, int(draft["bedrooms"]), key="wz_bedrooms")
        n_bath = c2.number_input("浴室", 1, 4, int(draft["bathrooms"]), key="wz_bathrooms")
        pings = list(draft["bedroom_ping"]) + [DEFAULT_BEDROOM_PING] * 6
        cols = st.columns(min(int(n_bed), 3))
        bed_ping = [cols[i % len(cols)].number_input(f"臥室 {i + 1}（坪）", 1.0, 20.0, float(pings[i]), 0.5,
                                                     key=f"wz_bed_ping_{i}") for i in range(int(n_bed))]
        c1, c2 = st.columns(2)
        living = c1.number_input("客廳（坪）", 2.0, 30.0, float(draft["living_ping"] or 8.0), 0.5, key="wz_living")
        kitchen = c2.number_input("廚房（坪）", 1.0, 15.0, float(draft["kitchen_ping"] or 3.0), 0.5, key="wz_kitchen")
        values = {"bedrooms": n_bed, "bathrooms": n_bath, "bedroom_ping": bed_ping, "living_ping": living,
                  "kitchen_ping": kitchen}
    elif step == 1:
        st.caption("成員人數決定誰在什麼時間待在哪個房間；大人住第一間臥室，小孩依序分配其他臥室。")
        c1, c2 = st.columns(2)
        adults = c1.number_input("大人", 1, 6, int(draft["adults"]), key="wz_adults")
        children = c2.number_input("小孩", 0, 6, int(draft["children"]), key="wz_children")
        st.caption("平日作息（假日沿用預設：睡到比較晚、白天在家）：")
        adult_times = _times_editor("大人平日", draft["adult_times"], "adult")
        child_times = _times_editor("小孩平日", draft["child_times"], "child") if children else draft["child_times"]
        values = {"adults": adults, "children": children, "adult_times": adult_times, "child_times": child_times}
    elif step == 2:
        plans = Tariff().plans
        ids = list(plans)
        plan_id = st.radio("電價方案", ids, index=ids.index(draft["plan_id"]) if draft["plan_id"] in ids else 0,
                           format_func=lambda p: plans[p]["name"], key="wz_plan")
        st.info(PLAN_TIPS.get(plan_id, ""), icon="💡")
        st.caption("不確定的話先選預設，之後可在左側「💰 電價方案」試算三種方案的月電費再決定。")
        values = {"plan_id": plan_id}
    else:
        choice = st.radio("家裡的熱水器", ["electric", "gas"], index=0 if draft["water_heater"] == "electric" else 1,
                          format_func={"electric": "電熱水器（儲熱式，計入用電）",
                                       "gas": "瓦斯熱水器（不計入用電）"}.get, key="wz_heater")
        st.caption("台灣多數家庭使用瓦斯熱水器；電熱水器冬天每天約多 5 度電，可排在離峰時段加熱。")
        values = {"water_heater": choice}

    st.divider()
    c1, c2, c3 = st.columns([1, 1, 1])
    if step > 0:
        c1.button("← 上一步", on_click=lambda: (_save(**values), _go(-1)), width="stretch")
    elif c1.button("略過引導", width="stretch", help="之後可從左側選單重新開啟"):
        st.session_state.ui = {**st.session_state.ui, "onboarded": True}
        _reset()
        st.rerun()
    if step < len(STEPS) - 1:
        c3.button("下一步 →", type="primary", on_click=lambda: (_save(**values), _go(1)), width="stretch")
    elif c3.button("完成", type="primary", width="stretch"):
        _save(**values)
        try:
            new, ui = apply_wizard(st.session_state.settings, st.session_state.ui, st.session_state.wizard)
            build_house(new)
        except (ValueError, KeyError) as e:
            st.error(f"設定有誤：{e}")
            return
        st.session_state.settings = new
        st.session_state.ui = ui
        st.session_state.flash = "已完成新手引導，所有結果依新設定重新計算"
        _reset()
        st.rerun()


def _reset() -> None:
    st.session_state.pop("wizard", None)
    st.session_state.pop("wizard_step", None)
    st.session_state.wizard_open = False
