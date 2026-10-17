"""A2：房間之間的隔間熱交換（RC 熱網路）。"""
from datetime import date

import pytest

from core.data_source import CwaCsvWeatherSource
from core.load_model import LoadModel, default_adjacency, load_params
from core.models import load_house
from core.schedule import Schedule
from ui.settings import adjacency, default_settings

SUMMER = date(2026, 7, 20)


@pytest.fixture(scope="module")
def model():
    house = load_house()
    return LoadModel(house, Schedule(house))


def _weather(model):
    return model.with_solar_geometry(CwaCsvWeatherSource().get_hourly_weather(SUMMER), SUMMER)


def test_default_adjacency_connects_rooms_to_living_room(model):
    adj = default_adjacency(model.house, load_params())
    assert ("bedroom_1", "living_room") in adj and all("living_room" in pair for pair in adj)
    assert not any("utility" in pair for pair in adj)                      # 公共設備沒有面積
    ua = model.interzone_ua["bedroom_1"]["living_room"]
    assert ua == pytest.approx(load_params()["interzone"]["u_partition"] * adj[("bedroom_1", "living_room")] * 2.7)


def test_layout_adjacency_from_floorplan():
    adj = adjacency(default_settings())
    assert adj[("bedroom_1", "master_bedroom")] > 3                         # 自動排列：主臥與次臥 1 並排
    assert all(length > 0 for length in adj.values())
    assert adj == adjacency(default_settings())                             # 同設定結果相同（快取 key 穩定）


def test_hot_neighbour_increases_cooling_and_cool_neighbour_decreases(model):
    room = model.house.get_space("bedroom_2")
    on = [1.0] * 24
    w = _weather(model)
    alone = model.space_hourly(room, w, "weekday", 26, ac_on=on)["air_conditioner"].sum()
    hot = model.space_hourly(room, w, "weekday", 26, ac_on=on, neighbor_temps={"living_room": [32.0] * 24})
    cool = model.space_hourly(room, w, "weekday", 26, ac_on=on, neighbor_temps={"living_room": [24.0] * 24})
    assert hot["air_conditioner"].sum() > alone > cool["air_conditioner"].sum()
    assert hot["interzone_w"].mean() > 0 > cool["interzone_w"].mean()
    # 不是鄰居的房間不影響
    other = model.space_hourly(room, w, "weekday", 26, ac_on=on, neighbor_temps={"kitchen": [40.0] * 24})
    assert other["air_conditioner"].sum() == pytest.approx(alone)


def test_free_running_room_drifts_toward_neighbour(model):
    room = model.house.get_space("bedroom_2")
    off = [0.0] * 24
    w = _weather(model)
    base = model.space_hourly(room, w, "weekday", ac_on=off)["room_temp_c"]
    cooled = model.space_hourly(room, w, "weekday", ac_on=off, neighbor_temps={"living_room": [24.0] * 24})
    assert (cooled["room_temp_c"] < base).all()                            # 隔壁開冷氣，這間也跟著涼一點
