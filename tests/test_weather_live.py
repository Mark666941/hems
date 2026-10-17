"""第 6b 批：Open-Meteo 即時天氣與地區設定（用假的下載函式，離線可執行）。"""
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from core.data_source import (
    CwaCsvWeatherSource,
    OpenMeteoWeatherSource,
    WeatherUnavailable,
    full_months,
)
from ui.settings import (
    build_analyzer,
    default_settings,
    location_coords,
    location_key,
    location_label,
    location_of,
    weather_source,
)

TODAY = date(2026, 10, 17)


class FakeOpenMeteo:
    """模擬 Open-Meteo 回應：時間 t 的氣溫 = 小時數、日射 = 100 × 小時數（白天），方便檢查對齊。"""

    def __init__(self):
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url, params["start_date"], params["end_date"]))
        start, end = date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])
        times = pd.date_range(start, end + timedelta(days=1), freq="h", inclusive="left")
        return {"hourly": {
            "time": [t.strftime("%Y-%m-%dT%H:%M") for t in times],
            "temperature_2m": [float(t.hour) for t in times],
            "relative_humidity_2m": [70.0] * len(times),
            "shortwave_radiation": [100.0 * t.hour if 6 <= t.hour <= 18 else 0.0 for t in times],
        }}


@pytest.fixture
def fake():
    return FakeOpenMeteo()


@pytest.fixture
def source(tmp_path, fake):
    return OpenMeteoWeatherSource(24.15, 120.68, cache_dir=tmp_path, fetch=fake, today=TODAY)


def test_hour_alignment_and_units(source):
    df = source.get_hourly_weather(date(2026, 7, 20))
    assert list(df.columns) == ["hour", "temp_c", "rh_pct", "solar_mj_m2"] and len(df) == 24
    # hour h 取 Open-Meteo 時間 h+1:00 的值（與中央氣象署「h:00–h+1:00」的慣例一致）
    assert df.loc[5, "temp_c"] == 6 and df.loc[23, "temp_c"] == 0
    assert df.loc[11, "solar_mj_m2"] == pytest.approx(1200 * 0.0036)      # W/m² 一小時平均 → MJ/m²


def test_archive_for_past_forecast_for_recent(source, fake):
    source.get_hourly_weather(date(2026, 7, 20))
    assert all("archive" in url for url, *_ in fake.calls)                # 過去：再分析資料
    fake.calls.clear()
    source.get_hourly_weather(TODAY + timedelta(days=3))                    # 本月：前段歷史 + 近期與預報
    urls = [url for url, *_ in fake.calls]
    assert any("archive" in u for u in urls) and any("api.open-meteo.com/v1/forecast" in u for u in urls)
    forecast = next(c for c in fake.calls if "forecast" in c[0])
    assert forecast[2] == (TODAY + timedelta(days=8)).isoformat()          # 預報 7 天＋多一天對齊用


def test_cache_reused_and_refreshed(source, fake, tmp_path):
    source.get_hourly_weather(date(2026, 7, 20))
    n = len(fake.calls)
    fresh = OpenMeteoWeatherSource(24.15, 120.68, cache_dir=tmp_path, fetch=fake, today=TODAY)
    fresh.get_hourly_weather(date(2026, 7, 21))
    assert len(fake.calls) == n                                            # 過去月份讀快取，不重新下載
    fresh.get_hourly_weather(TODAY)
    m = len(fake.calls)
    path = next(tmp_path.glob("*2026-10.csv"))
    old = datetime.now().timestamp() - 4 * 3600                            # 近期資料超過 3 小時要更新
    import os
    os.utime(path, (old, old))
    OpenMeteoWeatherSource(24.15, 120.68, cache_dir=tmp_path, fetch=fake, today=TODAY).get_hourly_weather(TODAY)
    assert len(fake.calls) > m


def test_available_dates_and_errors(source):
    days = source.available_dates()
    assert days[0] == date(2026, 1, 1) and days[-1] == TODAY + timedelta(days=7)
    with pytest.raises(WeatherUnavailable):
        source.get_hourly_weather(TODAY + timedelta(days=30))
    broken = OpenMeteoWeatherSource(24.15, 120.68, cache_dir=source.cache_dir / "x",
                                    fetch=lambda u, p: {"error": True, "reason": "bad"}, today=TODAY)
    with pytest.raises(WeatherUnavailable, match="bad"):
        broken.get_hourly_weather(date(2026, 3, 1))


def test_full_months_only_complete():
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(30 + 24)]   # 9 月全部 + 10/1–10/24
    assert full_months(days) == [(2026, 9)]
    assert full_months(CwaCsvWeatherSource().available_dates()) == [(2026, m) for m in range(1, 10)]


def test_location_settings():
    s = default_settings()
    assert location_of(s)["source"] == "cwa" and isinstance(weather_source(s), CwaCsvWeatherSource)
    assert location_label(s) == "臺中市｜中央氣象署臺中測站"
    old = {k: v for k, v in s.items() if k != "location"}                  # 舊設定檔沒有地區
    assert location_of(old) == location_of(s)
    s["location"] = {"source": "openmeteo", "county": "臺北市", "town": "大安區"}
    assert location_label(s) == "臺北市大安區｜Open-Meteo"
    src = weather_source(s)
    assert isinstance(src, OpenMeteoWeatherSource) and (src.lat, src.lon) == pytest.approx(location_coords(s), abs=1e-3)
    assert location_key(s) != location_key(default_settings())


def test_analyzer_uses_selected_location():
    s = default_settings()
    s["location"] = {"source": "openmeteo", "county": "臺北市", "town": "大安區"}
    analyzer = build_analyzer(s)
    assert isinstance(analyzer.sim.weather, OpenMeteoWeatherSource)
    site = analyzer.sim.load_model.params["site"]
    assert (site["latitude"], site["longitude"]) == pytest.approx(location_coords(s))   # 太陽位置用所選地區
    assert build_analyzer(default_settings()).sim.load_model.params["site"]["latitude"] != site["latitude"]
