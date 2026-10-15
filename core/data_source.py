"""資料來源介面：模擬層只透過這裡取得外部輸入（天氣、電價）。

目前使用中央氣象署歷史觀測 CSV；未來接入感測器時，只需新增一個
WeatherSource 子類別（例如讀取即時溫濕度計），模擬層的程式不必修改。
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path

import pandas as pd

from core.tariff import Tariff

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
WEATHER_DIR = ROOT / "data" / "weather"

WEATHER_COLUMNS = ["hour", "temp_c", "rh_pct", "solar_mj_m2"]


class WeatherSource(ABC):
    """天氣資料來源的共同介面。"""

    @abstractmethod
    def get_hourly_weather(self, day: date) -> pd.DataFrame:
        """回傳 24 列：hour、temp_c（°C）、rh_pct（%）、solar_mj_m2（全天空日射量 MJ/m²）。"""


class CwaCsvWeatherSource(WeatherSource):
    """讀取 scripts/fetch_cwa_weather.py 下載的中央氣象署逐時觀測 CSV。"""

    def __init__(self, station: str = "467490", data_dir: Path = WEATHER_DIR):
        self.station = station
        self.data_dir = data_dir

    def load_month(self, year: int, month: int) -> pd.DataFrame:
        path = self.data_dir / f"{self.station}_{year}-{month:02d}.csv"
        if not path.exists():
            raise FileNotFoundError(
                f"找不到 {path.name}，請先執行：python scripts/fetch_cwa_weather.py {year}-{month:02d}"
            )
        return pd.read_csv(path)

    def get_hourly_weather(self, day: date) -> pd.DataFrame:
        df = self.load_month(day.year, day.month)
        df = df[df["date"] == day.isoformat()].sort_values("hour")
        if list(df["hour"]) != list(range(24)):
            raise ValueError(f"{day} 的逐時資料不是完整的 0–23 時（實際 {len(df)} 筆）")
        return df[WEATHER_COLUMNS].reset_index(drop=True)


def pick_representative_day(month_df: pd.DataFrame, tariff: Tariff) -> str:
    """從一個月的逐時資料挑出代表日。

    條件：平日（非週末、非離峰日）、日降雨量 < 1 mm；
    在符合條件的日子中，選「日均溫」與「日最高溫」最接近該月平均的一天。
    """
    daily = month_df.groupby("date").agg(
        mean=("temp_c", "mean"), tmax=("temp_c", "max"), rain=("precip_mm", "sum")
    )
    target_mean, target_max = daily["mean"].mean(), daily["tmax"].mean()
    is_weekday = pd.Series(
        [not tariff.is_off_peak_day(date.fromisoformat(d)) for d in daily.index], index=daily.index
    )
    candidates = daily[is_weekday & (daily["rain"] < 1)]
    score = (candidates["mean"] - target_mean).abs() + (candidates["tmax"] - target_max).abs()
    return score.idxmin()


def load_representative_days(path: Path = CONFIG_DIR / "weather.json") -> dict[str, date]:
    """讀取 weather.json 中的季節代表日，例如 {"summer": date(2026, 7, 13), ...}。"""
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    return {k: date.fromisoformat(v["date"]) for k, v in config["representative_days"].items()}


def get_hourly_inputs(
    day: date,
    plan_id: str = "tou_2",
    weather: WeatherSource | None = None,
    tariff: Tariff | None = None,
) -> pd.DataFrame:
    """給定日期，回傳 24 小時的天氣與電價表，是模擬層唯一的外部輸入入口。"""
    weather = weather or CwaCsvWeatherSource()
    tariff = tariff or Tariff()
    df = weather.get_hourly_weather(day).merge(tariff.hourly_prices(day, plan_id), on="hour")
    df["period_label"] = df["period"].map(lambda p: p.label)
    return df


if __name__ == "__main__":
    # 用法：python -m core.data_source            → 列出夏季、冬季代表日
    #       python -m core.data_source 2026-07-20 → 列出指定日期
    import sys

    pd.set_option("display.unicode.east_asian_width", True)
    tariff = Tariff()
    if len(sys.argv) > 1:
        days = {"指定日期": date.fromisoformat(sys.argv[1])}
    else:
        days = {{"summer": "夏季代表日", "winter": "冬季代表日"}[k]: d
                for k, d in load_representative_days().items()}

    for title, day in days.items():
        kind = "離峰日（全日離峰）" if tariff.is_off_peak_day(day) else "平日"
        season = "夏月" if tariff.is_summer(day) else "非夏月"
        print(f"\n=== {title}：{day}（{season}、{kind}）｜{tariff.plans['tou_2']['name']} ===")
        df = get_hourly_inputs(day, tariff=tariff)
        print(df.drop(columns="period").rename(columns={
            "hour": "時", "temp_c": "氣溫°C", "rh_pct": "濕度%", "solar_mj_m2": "日射MJ/m²",
            "price": "電價元/度", "period_label": "時段",
        }).to_string(index=False))
        print(f"日均溫 {df.temp_c.mean():.1f}°C，最高 {df.temp_c.max():.1f}°C，最低 {df.temp_c.min():.1f}°C")
    print(f"\n電價來源：{tariff.source['name']}（{tariff.source['effective_date']} 起實施，"
          f"{tariff.source['retrieved_date']} 查詢）")
