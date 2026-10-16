"""🗺️ 平面圖：拖曳家電與房間、遙控器操作、即時狀態列。"""
from __future__ import annotations

import streamlit as st

from ui.context import AppContext, room_comfort, update_settings
from ui.family import add_member, move_person, person_states, remove_member, rename_member, set_member_times
from ui.floorplan import (
    ADDABLE,
    add_appliance,
    build_payload,
    default_spot,
    floorplan,
    move_appliance,
    move_room,
    remote_command,
    remove_appliance,
    room_dims,
)
from ui.theme import page_header


def _status(ctx: AppContext, h: int, cumulative: bool = False) -> dict:
    """狀態列資料。cumulative=True（動畫播放的每一格）時，今日用電與電費為 0 時到該小時的累計。"""
    r = ctx.selected
    hourly = r.hourly.set_index("hour")
    upto = hourly.loc[:h] if cumulative else hourly
    return {
        "date": str(ctx.day), "weekday": f"{ctx.weekday}・{ctx.day_type}", "time": f"{h:02d}:00",
        "location": "臺中市｜中央氣象署臺中測站", "outdoor_temp": float(hourly.loc[h, "temp_c"]),
        "rh": float(hourly.loc[h, "rh_pct"]), "house_kw": float(hourly.loc[h, "kwh"]),
        "period": hourly.loc[h, "period_label"], "price": float(hourly.loc[h, "price"]),
        "today_kwh": float(upto["kwh"].sum()), "today_cost": float(upto["cost"].sum()),
        "mode": ctx.mode_schedule.name,
    }


def _payload_at(ctx: AppContext, h: int, cumulative: bool = False) -> dict:
    r = ctx.selected
    payload = build_payload(ctx.settings, ctx.house, ctx.layout, ctx.templates, r, ctx.plan, ctx.remote, h,
                            _status(ctx, h, cumulative), room_comfort(ctx.analyzer, r, h))
    payload["people"] = person_states(ctx.sim.schedule, ctx.house, r.day_type, h, ctx.person_overrides)
    return payload


def day_frames(ctx: AppContext) -> list[dict]:
    """一天 24 小時的動畫畫格（C2）：每小時各房間室溫、家電狀態、人物位置與狀態列。
    推演結果已經包含整天，這裡只是把每個小時的狀態整理好一次送到瀏覽器，播放時不用重算。"""
    frames = []
    for h in range(24):
        p = _payload_at(ctx, h, cumulative=True)
        frames.append({
            "hour": h, "status": p["status"], "people": p["people"],
            "rooms": {r["id"]: {"temp": r["temp"], "comfort": r["comfort"]} for r in p["rooms"]},
            "appliances": {a["key"]: {"on": a["on"], "watts": a["watts"], "setpoint": a["setpoint"],
                                      "manual": a["manual"]} for a in p["appliances"]},
        })
    return frames


def make_payload(ctx: AppContext, frames: bool = False) -> dict:
    """平面圖元件需要的資料（首頁的精簡版也用這一份）。frames=True 時另附一天 24 小時的動畫畫格。"""
    payload = _payload_at(ctx, ctx.hour)
    payload["day_type_label"] = "平日" if ctx.selected.day_type == "weekday" else "假日"
    if frames:
        payload["frames"] = day_frames(ctx)
    return payload


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
    elif action == "move_person":
        person = next(p for p in payload["people"] if p["id"] == event["member"])
        st.session_state.person_overrides = move_person(ctx.person_overrides, person["id"], ctx.hour,
                                                        event["room"], person["activity"])
        st.session_state.flash = f"{person['name']}這個小時改在{ctx.house.get_space(event['room']).name}（臨時變動）"
        st.rerun()
    elif action == "clear_person":
        st.session_state.person_overrides = {k: v for k, v in ctx.person_overrides.items()
                                             if v["member"] != event["member"]}
        st.rerun()
    elif action == "rename_member":
        update_settings(rename_member(settings, event["member"], event.get("name", "")), "已更新名字")
    elif action == "member_times":
        times = {k: v for k, v in (event.get("times") or {}).items() if v}
        update_settings(set_member_times(settings, event["member"], ctx.selected.day_type, times), "已更新個人作息")
    elif action == "add_member":
        update_settings(*add_member(settings, house, event.get("name", ""), event.get("role", "adult"),
                                    event.get("bedroom")))
    elif action == "remove_member":
        new, msg = remove_member(settings, event["member"])
        st.session_state.person_overrides = {k: v for k, v in ctx.person_overrides.items()
                                             if v["member"] != event["member"]}
        update_settings(new, msg)
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
    payload = make_payload(ctx, frames=True)
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
- **播放一天**：按上方「▶ 播放一天」，平面圖從 0 時播到 23 時（室溫、家電、人物位置逐時變化），可拖曳時間軸、
  切換速度；播放時不能拖曳或遙控，按「回到模擬時間」恢復操作。播放使用已算好的結果，不會重新推演。
""")
