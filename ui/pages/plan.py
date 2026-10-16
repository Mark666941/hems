"""🗺️ 平面圖：拖曳家電與房間、遙控器操作、即時狀態列。"""
from __future__ import annotations

import streamlit as st

from ui.context import AppContext, room_comfort, update_settings
from ui.floorplan import (ADDABLE, add_appliance, build_payload, default_spot, floorplan, move_appliance, move_room,
                          remote_command, remove_appliance, room_dims)
from ui.theme import page_header


def make_payload(ctx: AppContext) -> dict:
    """平面圖元件需要的資料（首頁的精簡版也用這一份）。"""
    r, h = ctx.selected, ctx.hour
    hourly = r.hourly.set_index("hour")
    status = {
        "date": str(ctx.day), "weekday": f"{ctx.weekday}・{ctx.day_type}", "time": f"{h:02d}:00",
        "location": "臺中市｜中央氣象署臺中測站", "outdoor_temp": float(hourly.loc[h, "temp_c"]),
        "rh": float(hourly.loc[h, "rh_pct"]), "house_kw": float(hourly.loc[h, "kwh"]),
        "period": hourly.loc[h, "period_label"], "price": float(hourly.loc[h, "price"]),
        "today_kwh": r.total_kwh, "today_cost": r.energy_cost, "mode": ctx.mode_schedule.name,
    }
    return build_payload(ctx.settings, ctx.house, ctx.layout, ctx.templates, r, ctx.plan, ctx.remote, h,
                         status, room_comfort(ctx.analyzer, r, h))


def handle_event(ctx: AppContext, event: dict | None, payload: dict) -> None:
    """處理平面圖送回來的操作（每個操作有唯一的 nonce，避免重新整理時重複執行）。"""
    if not event or event.get("nonce") == st.session_state.get("last_nonce"):
        return
    st.session_state.last_nonce = event["nonce"]
    settings, layout, house, templates = ctx.settings, ctx.layout, ctx.house, ctx.templates
    working = {**settings, "layout": layout}
    action = event.get("action")
    if action == "move_appliance":
        new, msg = move_appliance(working, house, layout, templates, event["key"], event["x"], event["y"])
        if msg and msg.startswith("已將"):                   # 搬到別的房間：取消原本的遙控設定
            st.session_state.remote.pop(event["key"], None)
        update_settings(new, msg)
    elif action == "move_room":
        update_settings(move_room(working, layout, event["id"], event["x"], event["y"]))
    elif action == "remote":
        state = next(a for a in payload["appliances"] if a["key"] == event["key"])
        st.session_state.remote = remote_command(ctx.remote, event["key"], event["command"], event.get("value"),
                                                 state, ctx.hour)
        st.rerun()
    elif action == "remove":
        space_id, appliance_id = event["key"].split("/")
        st.session_state.remote.pop(event["key"], None)
        update_settings(*remove_appliance(working, house, space_id, appliance_id))
    elif action == "add":
        room = layout["rooms"][event["room"]]
        w, d = room_dims(ctx.cfg_by_id[event["room"]], room, templates)
        spot = default_spot(event["type"], room["x"], room["y"], w, d)
        new, msg = add_appliance(working, house, event["room"], event["type"], spot=spot)
        if new is working:
            st.toast(msg, icon="⚠️")
        else:
            update_settings(new, msg)


def render(ctx: AppContext) -> None:
    page_header("FLOOR PLAN · 智慧平面圖", "拖曳家電、遙控操作", [
        (f"🕑 模擬時間 <b>{ctx.hour:02d}:00</b>", ""), (f"🎛️ {ctx.mode_schedule.name}", "")])
    payload = make_payload(ctx)
    handle_event(ctx, floorplan(payload), payload)

    with st.expander("這張圖怎麼用？"):
        st.markdown(f"""
- **房間**：牆線內標示寬 × 深（公尺）、坪數與這個時刻的室溫（綠＝舒適、橘＝偏熱、藍＝偏冷、灰＝無人）。
  房間尺寸在左側「格局與房間」修改，冷房負載會依新面積重算；切到「調整格局」可以拖曳房間排成實際格局。
- **家電圖示**：可新增{"、".join(ADDABLE.values())}。右上綠點＝運轉中，左上橘點＝遙控器手動控制，下方為這個時刻的用電（W）。
  拖到別的房間，家電就會搬過去，模擬（例如冷氣負責哪個房間）隨之重算。
- **遙控器**：點一下家電圖示開啟。操作從左側「模擬時間」那一小時起生效到當天結束，
  例如把時間調到 17:00、遠端打開主臥冷氣，就能看到「下班前預冷」對用電與室溫的影響。
- **狀態列**：日期、模擬時間、地點、室外溫濕度、目前電價時段、全屋即時用電、今日累計用電與電費。
""")
