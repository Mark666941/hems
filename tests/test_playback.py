"""C2：平面圖一天動畫播放（瀏覽器端播放已算好的 24 小時畫格，不重算）。"""
import json
from datetime import date
from pathlib import Path

import pytest

from core.models import load_space_templates
from core.optimizer import ModeSchedule
from ui.context import AppContext
from ui.floorplan import FRONTEND_DIR, ensure_layout
from ui.pages.plan import day_frames, make_payload
from ui.settings import build_analyzer, default_settings, sim_key


@pytest.fixture(scope="module")
def ctx():
    s = default_settings()
    a = build_analyzer(s)
    day = date(2026, 7, 20)
    opt = a.opt.optimize(day, "normal", "tou_2")
    templates = load_space_templates()
    return AppContext(settings=s, key=sim_key(s), analyzer=a, templates=templates,
                      layout=ensure_layout(s, a.sim.house, templates), day=day, day_type="平日", hour=19,
                      plan_id="tou_2", mode_schedule=ModeSchedule.single(a.opt.modes["normal"]),
                      base_plan=opt.plan, plan=opt.plan, selected=opt.result, remote={})


def test_frames_cover_the_day(ctx):
    frames = day_frames(ctx)
    assert [f["hour"] for f in frames] == list(range(24))
    live = make_payload(ctx)
    room_ids = {r["id"] for r in live["rooms"]}
    keys = {a["key"] for a in live["appliances"]}
    for f in frames:
        assert set(f["rooms"]) == room_ids and set(f["appliances"]) == keys
        assert f["status"]["time"] == f"{f['hour']:02d}:00"
    json.dumps(frames)                                    # 可以送到瀏覽器


def test_frame_at_sim_hour_matches_live_view(ctx):
    frames, live = day_frames(ctx), make_payload(ctx)
    f = frames[ctx.hour]
    for r in live["rooms"]:
        assert f["rooms"][r["id"]] == {"temp": r["temp"], "comfort": r["comfort"]}
    for a in live["appliances"]:
        assert f["appliances"][a["key"]]["watts"] == a["watts"]
    assert [p["room"] for p in f["people"]] == [p["room"] for p in live["people"]]
    assert f["status"]["house_kw"] == pytest.approx(live["status"]["house_kw"])


def test_frames_accumulate_daily_usage(ctx):
    frames = day_frames(ctx)
    cum = [f["status"]["today_kwh"] for f in frames]
    assert cum == sorted(cum)                                         # 累計只增不減
    assert cum[-1] == pytest.approx(ctx.selected.total_kwh)           # 23 時＝全天
    assert make_payload(ctx)["status"]["today_kwh"] == pytest.approx(ctx.selected.total_kwh)


def test_people_move_during_the_day(ctx):
    frames = day_frames(ctx)
    at_home = [sum(1 for p in f["people"] if p["room"]) for f in frames]
    assert at_home[3] == 4 and at_home[12] == 0                      # 半夜全家在家、平日中午外出


def test_payload_frames_only_when_requested(ctx):
    assert "frames" not in make_payload(ctx)                          # 首頁精簡版不需要
    assert len(make_payload(ctx, frames=True)["frames"]) == 24


def test_frontend_has_player_controls():
    html = (Path(FRONTEND_DIR) / "index.html").read_text(encoding="utf-8")
    for element in ('id="play"', 'id="scrub"', 'id="speed"', 'id="back-live"', "function applyFrame"):
        assert element in html
