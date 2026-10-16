"""📈 預測：本月與下月的用電、電費（第 5 批完成；目前先放頁面架構與說明）。"""
from __future__ import annotations

import streamlit as st

from ui.context import AppContext
from ui.theme import page_header, section, tips_card


def render(ctx: AppContext) -> None:
    page_header("FORECAST · 預測", "本月與下月用電、電費")
    st.info("預測功能製作中：完成「家電使用紀錄」後，會依你的使用習慣與天氣預測用電與電費。", icon="🛠️")
    section("即將提供")
    tips_card([
        ("📝", "<b>家電使用紀錄</b>：填寫每週固定習慣（例如電視平日 19–22 時），或每天記錄實際使用時間。"),
        ("🧾", "<b>帳單度數</b>：輸入台電帳單的度數，比對模擬與實際的差距，由你決定是否修正模型。"),
        ("📈", "<b>本月／下月預測</b>：用電度數與電費，並標示可能範圍（例如 1,650～1,820 元）。"),
        ("🌦️", "<b>天氣預報</b>：接上即時天氣後，用未來幾天的預報估算近期用電。"),
    ])
