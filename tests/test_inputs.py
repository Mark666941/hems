"""階段 2：天氣資料與電價設定。"""
from datetime import date

import pytest

from core.data_source import (
    CwaCsvWeatherSource,
    get_hourly_inputs,
    load_representative_days,
    pick_representative_day,
)
from core.tariff import Period, Tariff


@pytest.fixture(scope="module")
def tariff():
    return Tariff()


# ---------- 電價 ----------

def test_summer_weekday_two_tier(tariff):
    prices = tariff.hourly_prices(date(2026, 7, 20), "tou_2")  # 週一
    assert len(prices) == 24
    assert prices.loc[8, "price"] == 2.06 and prices.loc[8, "period"] is Period.OFF_PEAK
    assert prices.loc[9, "price"] == 5.16 and prices.loc[9, "period"] is Period.PEAK
    assert prices.loc[23, "price"] == 5.16


def test_non_summer_weekday_two_tier(tariff):
    prices = tariff.hourly_prices(date(2026, 1, 20), "tou_2")  # 週二
    # 非夏月中午 11–14 時是離峰
    assert prices.loc[5, "price"] == 1.99
    assert prices.loc[6, "price"] == 4.93
    assert prices.loc[12, "price"] == 1.99
    assert prices.loc[14, "price"] == 4.93


def test_three_tier_summer_peak(tariff):
    prices = tariff.hourly_prices(date(2026, 7, 20), "tou_3")
    assert prices.loc[17, "price"] == 7.13
    assert prices.loc[10, "period"] is Period.SEMI_PEAK
    assert prices.loc[22, "period"] is Period.SEMI_PEAK


def test_off_peak_days(tariff):
    assert tariff.is_off_peak_day(date(2026, 7, 18))      # 週六
    assert tariff.is_off_peak_day(date(2026, 10, 10))     # 國慶日（固定日期）
    assert tariff.is_off_peak_day(date(2026, 2, 17))      # 春節（逐年列出）
    assert not tariff.is_off_peak_day(date(2026, 7, 20))  # 一般週一
    prices = tariff.hourly_prices(date(2026, 7, 18), "tou_2")
    assert (prices["price"] == 2.06).all()


def test_missing_lunar_calendar_warns(tariff):
    with pytest.warns(UserWarning, match="2027"):
        tariff.is_off_peak_day(date(2027, 2, 8))


def test_progressive_bill_matches_taipower_example(tariff):
    # 台電範例：非時間住宅用戶 8 月用電 400 度，電費 1,015 元
    assert round(tariff.progressive_bill(400, summer=True)) == 1015


# ---------- 天氣 ----------

def test_representative_days_have_full_weather():
    src = CwaCsvWeatherSource()
    days = load_representative_days()
    summer = src.get_hourly_weather(days["summer"])
    winter = src.get_hourly_weather(days["winter"])
    assert len(summer) == len(winter) == 24
    assert summer.notna().all().all() and winter.notna().all().all()
    # 合理範圍：台中夏季白天比冬季熱，濕度介於 0–100%
    assert summer["temp_c"].mean() > winter["temp_c"].mean() + 5
    assert summer["rh_pct"].between(0, 100).all()
    # 夜間沒有日射
    assert summer.loc[0, "solar_mj_m2"] == 0


def test_representative_day_selection_is_reproducible(tariff):
    src = CwaCsvWeatherSource()
    days = load_representative_days()
    for d in days.values():
        assert pick_representative_day(src.load_month(d.year, d.month), tariff) == d.isoformat()


def test_get_hourly_inputs_combines_weather_and_price():
    df = get_hourly_inputs(date(2026, 7, 20))
    assert list(df["hour"]) == list(range(24))
    assert {"temp_c", "rh_pct", "price", "period_label"} <= set(df.columns)


def test_missing_month_gives_clear_error():
    with pytest.raises(FileNotFoundError, match="fetch_cwa_weather"):
        CwaCsvWeatherSource().get_hourly_weather(date(2020, 3, 1))
