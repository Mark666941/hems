"""📈 預測：家電使用紀錄（每週習慣、每日紀錄、帳單度數）＋本月／下月用電與電費預測。

「本月」以左側選單的模擬日期所在月份為準（目前天氣資料只到 2026 年 9 月）。
紀錄存在資料庫（每個設定檔各自一份），預測方法見 ui/usage.py。
"""
from __future__ import annotations

from datetime import date

import streamlit as st

from ui import charts, usage
from ui.account import get_store
from ui.context import AppContext
from ui.theme import kpi_cards, page_header, section, tips_card

KIND_LABELS = {"habit": "每週習慣", "daily": "每日紀錄", "bill": "帳單"}


def _records(store, user_id: int, profile_id: int) -> tuple[dict, dict]:
    """回傳（{種類: [內容]}, {種類: [(id, 內容)]}）。"""
    rows = {k: store.list_records(user_id, profile_id, k) for k in KIND_LABELS}
    return {k: [d for _, d in v] for k, v in rows.items()}, rows


def _range(lo: float, hi: float, fmt: str = "{:,.0f}") -> str:
    return f"{fmt.format(lo)}～{fmt.format(hi)}"


def render(ctx: AppContext) -> None:
    store = get_store()
    uid, pid = st.session_state.user_id, st.session_state.profile_id
    records, rows = _records(store, uid, pid)
    appliances = usage.recordable_appliances(ctx.analyzer)
    powers = {k: p for k, (_, p) in appliances.items()}
    tariff = ctx.tariff

    def profiles(year: int, month: int):
        return ctx.month_profile(year, month)

    this_ym = (ctx.day.year, ctx.day.month)
    next_ym = usage.next_month(*this_ym)
    bill_table, factor = usage.compare_bills(records["bill"], profiles, tariff, records, powers)
    applied = bool(st.session_state.ui.get("forecast_correction")) and factor is not None
    scale = factor if applied else 1.0
    results = []
    for ym in (this_ym, next_ym):
        proxy = ctx.month_profile(*ym)[1] != ym
        spread = usage.uncertainty(bill_table, factor, applied, proxy)
        results.append(usage.forecast_month(ym[0], ym[1], ctx.plan_id, profiles, tariff, records, powers,
                                            scale, spread))
    now, nxt = results

    page_header("FORECAST · 預測", "本月與下月用電、電費", [
        (f"📅 本月＝模擬日期 <b>{ctx.day:%Y-%m}</b>", ""),
        (f"💰 {tariff.plans[ctx.plan_id]['name']}", ""),
        (f"📝 紀錄 <b>{len(records['habit']) + len(records['daily'])}</b> 筆・帳單 <b>{len(records['bill'])}</b> 張", ""),
    ])

    def note(r: dict) -> str:
        parts = []
        if r["proxy"] and r["source"]:
            parts.append(f"尚無天氣資料，以 {r['source'][1]} 月天氣估算")
        if abs(r["record_kwh"]) >= 0.5:
            parts.append(f"使用紀錄 {r['record_kwh']:+.0f} 度")
        if applied:
            parts.append(f"帳單修正 ×{scale:.2f}")
        return "・".join(parts) or "物理模擬（一般習慣）"

    kpi_cards([
        {"label": f"{now['month']} 月預測用電", "value": f"{now['kwh']:,.0f}", "unit": "度",
         "sub": f"範圍 {_range(now['low'], now['high'])} 度・{note(now)}"},
        {"label": f"{now['month']} 月預測電費", "value": f"{now['bill']:,.0f}", "unit": "元",
         "sub": f"範圍 {_range(now['bill_low'], now['bill_high'])} 元"},
        {"label": f"{nxt['month']} 月預測用電", "value": f"{nxt['kwh']:,.0f}", "unit": "度",
         "sub": f"範圍 {_range(nxt['low'], nxt['high'])} 度・{note(nxt)}"},
        {"label": f"{nxt['month']} 月預測電費", "value": f"{nxt['bill']:,.0f}", "unit": "元",
         "sub": f"範圍 {_range(nxt['bill_low'], nxt['bill_high'])} 元"},
    ])
    st.caption("電費為單月估算（台電住宅帳單通常兩個月一期）；以一般習慣（未最佳化）推演，改用省電或正常模式還會再下降。")

    section("每日預測用電")
    month_tab = st.segmented_control("月份", [f"{now['month']} 月", f"{nxt['month']} 月"],
                                     default=f"{now['month']} 月", key="forecast_month")
    shown = nxt if month_tab == f"{nxt['month']} 月" else now
    st.altair_chart(charts.forecast_daily(shown["daily"]), width="stretch")

    section("帳單與模擬比對")
    if bill_table.empty:
        st.info("還沒有帳單。到下方「🧾 帳單度數」輸入台電帳單的度數，就能比對模擬與實際的差距。", icon="🧾")
    else:
        st.dataframe(bill_table, hide_index=True, width="stretch", column_config={
            "帳單度數": st.column_config.NumberColumn(format="%.0f 度"),
            "模擬度數": st.column_config.NumberColumn(format="%.0f 度"),
            "誤差": st.column_config.NumberColumn("帳單比模擬", format="percent")})
        total_err = factor - 1
        direction = "多" if total_err > 0 else "少"
        st.markdown(f"帳單合計比模擬**{direction} {abs(total_err) * 100:.1f}%**（修正係數 ×{factor:.2f}）。"
                    "差距可能來自沒有建模的家電、實際作息不同或天氣差異；是否修正由你決定。")
        want = st.toggle("依帳單修正預測", value=applied, key="forecast_correction_toggle",
                         help="開啟後，預測值乘上修正係數（帳單總度數 ÷ 同期間模擬度數）")
        if want != applied:
            st.session_state.ui["forecast_correction"] = want
            st.rerun()

    section("家電使用紀錄")
    st.caption("有紀錄的家電，預測時以「功率 × 使用時數」取代模擬的用電；每日紀錄優先於每週習慣。"
               "冷氣耗電隨天氣與設定溫度變化，由模擬計算，不在此記錄。")
    tab_habit, tab_daily, tab_bill = st.tabs(["📝 每週習慣", "📅 每日紀錄", "🧾 帳單度數"])
    with tab_habit:
        _habit_form(store, uid, pid, appliances)
        _record_list(store, uid, pid, rows["habit"], appliances, usage.describe_habit)
    with tab_daily:
        _daily_form(store, uid, pid, appliances, ctx.day)
        _record_list(store, uid, pid, rows["daily"], appliances,
                     lambda d: f"{d['date']} 使用 {float(d['hours']):g} 小時")
    with tab_bill:
        _bill_form(store, uid, pid, ctx.day)
        _record_list(store, uid, pid, rows["bill"], appliances, _describe_bill)

    with st.expander("ℹ️ 預測怎麼算"):
        tips_card([
            ("⚙️", ("<b>物理模擬</b>：依房型、作息與中央氣象署天氣，推演該月每一天的用電（一般習慣）；"
                    "沒有天氣資料的月份，用最接近月份的天氣、依平日／假日平均估算，電費仍依該月電價計算。")),
            ("📝", "<b>使用紀錄</b>：有紀錄的家電以「功率 × 紀錄時數」取代模擬值。"),
            ("🧾", "<b>帳單修正</b>：帳單總度數 ÷ 同期間模擬度數，開啟後預測乘上這個係數。"),
            ("📏", "<b>範圍</b>：沒有帳單時 ±10%；有帳單時依帳單與模擬的誤差（至少 ±5%）；以代替月份天氣估算時再加 5%。"),
        ])


# ---------- 表單 ----------

def _describe_bill(d: dict) -> str:
    amount = f"、{float(d['amount']):,.0f} 元" if d.get("amount") else ""
    return f"{d['start']}～{d['end']}：{float(d['kwh']):,.0f} 度{amount}"

def _appliance_select(appliances: dict, key: str) -> str:
    return st.selectbox("家電", list(appliances), format_func=lambda k: appliances[k][0], key=key)


def _habit_form(store, uid: int, pid: int, appliances: dict) -> None:
    with st.form("habit_form", clear_on_submit=False, border=False):
        target = _appliance_select(appliances, "habit_appliance")
        days = st.pills("星期", list(range(7)), format_func=lambda d: f"週{usage.WEEKDAY_NAMES[d]}",
                        selection_mode="multi", default=list(range(5)), key="habit_days")
        c1, c2 = st.columns(2)
        start = c1.selectbox("開始", list(range(24)), index=19, format_func=lambda h: f"{h:02d}:00", key="habit_start")
        end = c2.selectbox("結束", list(range(24)), index=22, format_func=lambda h: f"{h:02d}:00", key="habit_end")
        if st.form_submit_button("新增每週習慣", type="primary"):
            if not days:
                st.warning("請至少選一天")
            elif start == end:
                st.warning("開始與結束時間不能相同")
            else:
                sid, aid = target.split("/")
                store.add_record(uid, pid, "habit", {"space_id": sid, "appliance_id": aid,
                                                     "weekdays": sorted(days), "start": start, "end": end})
                st.session_state.flash = "已新增每週習慣"
                st.rerun()


def _daily_form(store, uid: int, pid: int, appliances: dict, default_day: date) -> None:
    with st.form("daily_form", border=False):
        target = _appliance_select(appliances, "daily_appliance")
        c1, c2 = st.columns(2)
        day = c1.date_input("日期", value=default_day, key="daily_date")
        hours = c2.number_input("使用時數", 0.0, 24.0, 2.0, 0.5, key="daily_hours")
        if st.form_submit_button("新增每日紀錄", type="primary"):
            sid, aid = target.split("/")
            store.add_record(uid, pid, "daily", {"date": day.isoformat(), "space_id": sid, "appliance_id": aid,
                                                 "hours": float(hours)})
            st.session_state.flash = "已新增每日紀錄"
            st.rerun()


def _bill_form(store, uid: int, pid: int, default_day: date) -> None:
    with st.form("bill_form", border=False):
        first = default_day.replace(day=1)
        c1, c2 = st.columns(2)
        start = c1.date_input("計費起日", value=first, key="bill_start")
        end = c2.date_input("計費迄日", value=default_day, key="bill_end")
        c1, c2 = st.columns(2)
        kwh = c1.number_input("用電度數", 0.0, 100000.0, 0.0, 1.0, key="bill_kwh")
        amount = c2.number_input("電費金額（元，選填）", 0.0, 1000000.0, 0.0, 1.0, key="bill_amount")
        st.caption("帳單上的「計費期間」與「用電度數」（台電住宅通常兩個月一期）。")
        if st.form_submit_button("新增帳單", type="primary"):
            if end < start:
                st.warning("迄日不能早於起日")
            elif kwh <= 0:
                st.warning("請輸入用電度數")
            else:
                store.add_record(uid, pid, "bill", {"start": start.isoformat(), "end": end.isoformat(),
                                                    "kwh": float(kwh), "amount": float(amount) or None})
                st.session_state.flash = "已新增帳單"
                st.rerun()


def _record_list(store, uid: int, pid: int, rows: list[tuple[int, dict]], appliances: dict, describe) -> None:
    if not rows:
        st.caption("尚無紀錄")
        return
    for rid, data in rows:
        c1, c2 = st.columns([6, 1])
        key = f"{data.get('space_id')}/{data.get('appliance_id')}"
        name = appliances[key][0].split("（")[0] + "：" if key in appliances else ""
        c1.markdown(f"{name}{describe(data)}")
        if c2.button("刪除", key=f"del_record_{rid}"):
            store.delete_record(uid, pid, rid)
            st.session_state.flash = "已刪除紀錄"
            st.rerun()
