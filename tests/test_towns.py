"""第 6b 批：鄉鎮市區中心點（config/towns.json）。"""
import json
from pathlib import Path

TOWNS = json.loads((Path(__file__).resolve().parent.parent / "config" / "towns.json").read_text(encoding="utf-8"))["towns"]


def test_all_counties_and_towns():
    assert len(TOWNS) == 22
    assert sum(len(t) for t in TOWNS.values()) == 368
    assert "北屯區" in TOWNS["臺中市"] and "大安區" in TOWNS["臺北市"]


def test_centroids_inside_taiwan():
    for towns in TOWNS.values():
        for lat, lon in towns.values():
            assert 21.8 < lat < 26.5 and 118.1 < lon < 122.1


def test_known_locations():
    lat, lon = TOWNS["臺北市"]["大安區"]
    assert abs(lat - 25.03) < 0.03 and abs(lon - 121.54) < 0.03
    lat, lon = TOWNS["臺中市"]["北屯區"]
    assert abs(lat - 24.18) < 0.05 and abs(lon - 120.74) < 0.05
