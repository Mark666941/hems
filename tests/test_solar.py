"""A3：太陽位置與各方位牆面日射（西曬）。"""
from datetime import date

import pytest

from core.data_source import CwaCsvWeatherSource
from core.solar import hourly_vertical, solar_position, split_beam_diffuse, vertical_irradiance

LAT, LON = 24.15, 120.68          # 臺中
SUMMER, WINTER = date(2026, 7, 20), date(2026, 1, 15)


def test_solar_position_known_values():
    alt, _ = solar_position(date(2026, 6, 21), 12.0, LAT, LON)
    assert alt == pytest.approx(90 - (LAT - 23.45), abs=1.5)        # 夏至中午 ≈ 90 − (緯度 − 23.45)
    alt, az = solar_position(date(2026, 12, 21), 12.0, LAT, LON)
    assert alt == pytest.approx(90 - (LAT + 23.45), abs=1.5) and 170 < az < 190   # 冬至中午在正南
    _, az_am = solar_position(SUMMER, 9, LAT, LON)
    _, az_pm = solar_position(SUMMER, 16, LAT, LON)
    assert az_am < 180 < az_pm                                         # 上午在東、下午在西
    assert solar_position(SUMMER, 2, LAT, LON)[0] < 0                  # 半夜太陽在地平線下


def test_beam_diffuse_split_conserves_energy():
    beam, diffuse = split_beam_diffuse(600, 1000)
    assert beam + diffuse == pytest.approx(600) and beam > diffuse      # 晴天直射為主
    beam, diffuse = split_beam_diffuse(100, 1000)
    assert diffuse > 0.9 * 100                                          # 陰天幾乎都是散射


def test_vertical_irradiance_physical_limits():
    for ghi, alt in [(800, 60), (300, 10), (80, 3)]:
        for facing in (0, 90, 180, 270):
            assert 0 <= vertical_irradiance(ghi, alt, 270, 1300 * alt / 90, facing) <= 1100


def _daily(day, orientation):
    w = CwaCsvWeatherSource().get_hourly_weather(day)
    return hourly_vertical(day, [s * 1e6 / 3600 for s in w.solar_mj_m2], LAT, LON, orientation)


def test_west_sun_in_summer_south_sun_in_winter():
    west, north, south = (sum(_daily(SUMMER, o)) for o in ("W", "N", "S"))
    assert west > 1.5 * max(north, south)                               # 夏季西曬
    w = _daily(SUMMER, "W")
    assert w.index(max(w)) >= 14                                        # 西向牆面下午最熱
    assert sum(_daily(WINTER, "S")) > 1.5 * sum(_daily(WINTER, "N"))     # 冬季南向日照最多


def test_west_facing_room_needs_more_cooling():
    from core.load_model import LoadModel
    from core.models import load_house
    from core.schedule import Schedule
    house = load_house()
    model = LoadModel(house, Schedule(house))
    space = house.get_space("bedroom_1")
    weather = CwaCsvWeatherSource().get_hourly_weather(SUMMER)
    kwh = {}
    for o in ("W", "N"):
        space.orientation = o
        w = model.with_solar_geometry(weather, SUMMER)
        on = [1.0] * 24                                                  # 整天開冷氣比較方位影響
        kwh[o] = model.space_hourly(space, w, "weekday", 26, ac_on=on)["air_conditioner"].sum() / 1000
    assert kwh["W"] > kwh["N"] * 1.08


def test_orientation_in_spaces_table_roundtrip():
    from ui.settings import apply_spaces_table, build_house, default_settings, spaces_table
    s = default_settings()
    rows = spaces_table(s)
    assert {r["名稱"]: r["窗戶方位"] for r in rows}["次臥 1"] == "西"
    rows = [{**r, "窗戶方位": "東"} if r["名稱"] == "次臥 1" else r for r in rows]
    rows = [{**r, "窗戶方位": "未指定"} if r["名稱"] == "主臥" else r for r in rows]
    house = build_house(apply_spaces_table(s, rows))
    assert house.get_space("bedroom_1").orientation == "E"
    assert house.get_space("master_bedroom").orientation is None
