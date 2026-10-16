"""⚙️ 設定：房型、家電功率、作息、比較基準（較少用、欄位較多的設定放這裡；常用設定在左側選單）。"""
from __future__ import annotations

import copy
from datetime import time

import pandas as pd
import streamlit as st

from ui.context import AppContext, update_settings
from ui.settings import (ROUTINE_LABELS, apply_routine_times, apply_spaces_table, build_house, default_settings,
                         routine_times, spaces_table)
from ui.theme import page_header


def _to_time(text: str | None, fallback: str) -> time:
    h, m = map(int, (text or fallback).replace("24:00", "23:59").split(":"))
    return time(h, m)


def render(ctx: AppContext) -> None:
    settings, templates = ctx.settings, ctx.templates
    page_header("SETTINGS · 設定", "房型、家電與作息")
    st.caption("修改後按「套用」，所有結果會依新設定重新計算。常用設定（日期、電價、模式、舒適偏好）在左側選單。")

    with st.expander("🏠 房型：空間範本與面積", expanded=True):
        st.caption("可新增或刪除列。新增的空間依範本帶入預設家電；家庭成員使用的臥室、浴室若被刪除會提示錯誤。")
        edited = st.data_editor(
            pd.DataFrame(spaces_table(settings)), num_rows="dynamic", hide_index=True, key="spaces_editor",
            width="stretch",
            column_config={
                "id": st.column_config.TextColumn("代碼", disabled=True, help="系統自動產生"),
                "範本": st.column_config.SelectboxColumn(options=list(templates), required=True,
                                                       help="、".join(f"{k}={v['name']}" for k, v in templates.items())),
                "坪數": st.column_config.NumberColumn(min_value=0.0, max_value=50.0, step=0.5),
            })
        if st.button("套用房型"):
            update_settings(apply_spaces_table(settings, edited.to_dict("records")), "已更新房型")

    with st.expander("🔌 家電功率與啟用"):
        house = build_house(settings)
        rows = [{"key": f"{s.id}/{a.id}", "空間": s.name, "家電": a.name, "類型": a.load_type.label,
                 "功率(W)": a.power_w, "啟用": a.enabled} for s, a in house.all_appliances()]
        edited_app = st.data_editor(pd.DataFrame(rows), hide_index=True, key="appliance_editor", width="stretch",
                                    disabled=["key", "空間", "家電", "類型"],
                                    column_order=["空間", "家電", "類型", "功率(W)", "啟用"],
                                    column_config={"功率(W)": st.column_config.NumberColumn(min_value=0, step=10)})
        st.caption("冷氣的功率為額定耗電，冷房能力 = 功率 × 額定 COP。熱水器改用瓦斯時，取消勾選電熱水器。")
        if st.button("套用家電設定"):
            new = copy.deepcopy(settings)
            new["appliance_edits"] = {r["key"]: {"power_w": r["功率(W)"], "enabled": bool(r["啟用"])}
                                      for r in edited_app.to_dict("records")}
            update_settings(new, "已更新家電設定")

    with st.expander("🕒 作息時間"):
        st.caption("調整起床、就寢、外出時間；盥洗、三餐、洗澡等時段沿用預設。")
        with st.form("routine_form"):
            new = copy.deepcopy(settings)
            for rid, label in ROUTINE_LABELS.items():
                t = routine_times(settings["schedule"]["routines"][rid])
                st.markdown(f"**{label}**")
                c = st.columns(4)
                wake = c[0].time_input("起床", _to_time(t["wake"], "07:00"), key=f"{rid}_wake", step=900)
                bed = c[1].time_input("就寢", _to_time(t["bed"], "23:00"), key=f"{rid}_bed", step=900)
                out_s = c[2].time_input("外出", _to_time(t["out_start"], "08:00"), key=f"{rid}_out_s", step=900)
                out_e = c[3].time_input("回家", _to_time(t["out_end"], "18:00"), key=f"{rid}_out_e", step=900)
                new = apply_routine_times(new, rid, {"wake": wake.strftime("%H:%M"), "bed": bed.strftime("%H:%M"),
                                                     "out_start": out_s.strftime("%H:%M"),
                                                     "out_end": out_e.strftime("%H:%M")})
            st.markdown("**成員的臥室與浴室**")
            space_names = {s["id"]: s.get("name", s["id"]) for s in settings["house"]["spaces"]}
            ids = list(space_names)
            for i, member in enumerate(new["schedule"]["members"]):
                c = st.columns(3)
                c[0].markdown(f"{member['name']}")
                member["bedroom"] = c[1].selectbox("臥室", ids, index=ids.index(member["bedroom"]) if member["bedroom"] in ids else 0,
                                                   format_func=space_names.get, key=f"m{i}_bed")
                member["bathroom"] = c[2].selectbox("浴室", ids, index=ids.index(member["bathroom"]) if member["bathroom"] in ids else 0,
                                                    format_func=space_names.get, key=f"m{i}_bath")
            if st.form_submit_button("套用作息"):
                update_settings(new, "已更新作息")

    with st.expander("📏 比較基準"):
        base_sp = st.number_input("一般習慣的冷氣設定溫度（°C）", 16.0, 30.0, float(settings["baseline_setpoint_c"]), 0.5,
                                  format="%.1f",
                                  help="基準情境：有人就開冷氣，整天固定這個溫度；可移轉負載在晚間習慣時段運轉。")
        if base_sp != settings["baseline_setpoint_c"]:
            new = copy.deepcopy(settings)
            new["baseline_setpoint_c"] = base_sp
            update_settings(new, "已更新比較基準")

    st.divider()
    if st.button("恢復全部預設設定"):
        update_settings(default_settings(), "已恢復預設設定")
