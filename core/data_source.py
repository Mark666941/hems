"""資料來源介面：模擬層只透過這裡取得外部輸入（天氣、電價）。

天氣有兩種來源：中央氣象署臺中測站歷史觀測 CSV（離線可用），以及 Open-Meteo（任一地點、含未來 7 天預報）。
未來接入感測器時，只需新增一個 WeatherSource 子類別（例如讀取即時溫濕度計），模擬層的程式不必修改。
"""
from __future__ import annotations

import calendar
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
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

    def available_dates(self) -> list[date]:
        """已下載的所有日期（介面的日期選單用）。"""
        days = []
        for path in sorted(self.data_dir.glob(f"{self.station}_*.csv")):
            days += [date.fromisoformat(d) for d in pd.read_csv(path, usecols=["date"])["date"].unique()]
        return sorted(days)

    def get_hourly_weather(self, day: date) -> pd.DataFrame:
        df = self.load_month(day.year, day.month)
        df = df[df["date"] == day.isoformat()].sort_values("hour")
        if list(df["hour"]) != list(range(24)):
            raise ValueError(f"{day} 的逐時資料不是完整的 0–23 時（實際 {len(df)} 筆）")
        return df[WEATHER_COLUMNS].reset_index(drop=True)


class WeatherUnavailable(RuntimeError):
    """取不到天氣資料（沒有網路、超出可查詢的日期範圍等），訊息可直接顯示給使用者。"""


def _http_json(url: str, params: dict, timeout: float = 20) -> dict:
    query = urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(f"{url}?{query}", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise WeatherUnavailable(f"無法連線到 Open-Meteo 取得天氣（{e}）。請確認網路，或改用中央氣象署臺中測站資料。") from e


class OpenMeteoWeatherSource(WeatherSource):
    """Open-Meteo 逐時天氣（免金鑰）：任一經緯度，過去用再分析資料、近期與未來 7 天用預報。

    - 歷史（今天 5 天以前）：archive-api（ERA5 等再分析資料，約 5 天延遲）
    - 近期與預報：forecast API（可查過去約 3 個月到未來 16 天）
    下載後依「地點＋月份」存成 CSV 快取；含近期或未來日期的月份，超過 forecast_ttl_h 小時就重新下載。

    時間對齊：中央氣象署逐時資料的 hour = h 代表 h:00–h+1:00（觀測時間 h+1:00），
    Open-Meteo 的 shortwave_radiation 是「前一小時平均」，所以 hour h 取 Open-Meteo 時間 h+1:00 的值，兩種來源一致。
    """

    ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
    VARIABLES = "temperature_2m,relative_humidity_2m,shortwave_radiation"
    FIRST_DAY = date(2026, 1, 1)      # 與中央氣象署資料同起點（電價、作息設定以 2026 年為準）
    ARCHIVE_LAG_DAYS = 5
    FORECAST_DAYS = 7

    def __init__(self, latitude: float, longitude: float, cache_dir: Path | None = None,
                 fetch=_http_json, today: date | None = None, forecast_ttl_h: float = 3.0):
        self.lat, self.lon = round(latitude, 3), round(longitude, 3)
        # 快取資料夾：參數 > 環境變數 HEMS_WEATHER_CACHE（安裝版放使用者資料夾）> data/weather/openmeteo
        self.cache_dir = cache_dir or Path(os.environ.get("HEMS_WEATHER_CACHE") or WEATHER_DIR / "openmeteo")
        self.fetch = fetch                   # 測試時換成假的下載函式
        self.today = today
        self.ttl_h = forecast_ttl_h
        self._months: dict[tuple[int, int], pd.DataFrame] = {}

    def _today(self) -> date:
        return self.today or date.today()

    def available_dates(self) -> list[date]:
        last = self._today() + timedelta(days=self.FORECAST_DAYS)
        return [self.FIRST_DAY + timedelta(days=i) for i in range((last - self.FIRST_DAY).days + 1)]

    def _cache_path(self, year: int, month: int) -> Path:
        return self.cache_dir / f"openmeteo_{self.lat:.3f}_{self.lon:.3f}_{year}-{month:02d}.csv"

    def _download(self, start: date, end: date) -> pd.DataFrame:
        """下載 [start, end] 的逐時資料（多抓一天，供最後一小時對齊用）。"""
        archive_end = self._today() - timedelta(days=self.ARCHIVE_LAG_DAYS)
        parts = []
        ranges = []
        if start <= archive_end:
            ranges.append((self.ARCHIVE_URL, start, min(end + timedelta(days=1), archive_end)))
        if end + timedelta(days=1) > archive_end:
            ranges.append((self.FORECAST_URL, max(start, archive_end + timedelta(days=1)), end + timedelta(days=1)))
        for url, a, b in ranges:
            data = self.fetch(url, {"latitude": self.lat, "longitude": self.lon, "hourly": self.VARIABLES,
                                    "timezone": "Asia/Taipei", "start_date": a.isoformat(), "end_date": b.isoformat()})
            if "hourly" not in data:
                raise WeatherUnavailable(f"Open-Meteo 回傳錯誤：{data.get('reason', data)}")
            h = data["hourly"]
            parts.append(pd.DataFrame({"time": pd.to_datetime(h["time"]), "temp_c": h["temperature_2m"],
                                       "rh_pct": h["relative_humidity_2m"], "rad_w_m2": h["shortwave_radiation"]}))
        raw = pd.concat(parts).drop_duplicates("time").set_index("time").sort_index()
        raw = raw.apply(pd.to_numeric, errors="coerce").interpolate(limit_direction="both")
        # 對齊：hour h 取 h+1:00 的值（最後一筆沒有下一小時時沿用前一小時）
        shifted = raw.shift(-1).ffill()
        shifted = shifted[(shifted.index.date >= start) & (shifted.index.date <= end)]
        return pd.DataFrame({
            "date": [t.date().isoformat() for t in shifted.index], "hour": shifted.index.hour,
            "temp_c": shifted["temp_c"].round(1).values, "rh_pct": shifted["rh_pct"].round(0).values,
            "solar_mj_m2": (shifted["rad_w_m2"] * 0.0036).round(3).values,     # W/m² 一小時平均 → MJ/m²
        })

    def load_month(self, year: int, month: int) -> pd.DataFrame:
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        last = min(last, self._today() + timedelta(days=self.FORECAST_DAYS))
        if last < first or first < self.FIRST_DAY:
            raise WeatherUnavailable(f"Open-Meteo 天氣只提供 {self.FIRST_DAY} 到今天之後 {self.FORECAST_DAYS} 天")
        recent = last >= self._today() - timedelta(days=self.ARCHIVE_LAG_DAYS)
        path = self._cache_path(year, month)
        if path.exists():
            age_h = (datetime.now().timestamp() - path.stat().st_mtime) / 3600
            cached = pd.read_csv(path)
            if (not recent or age_h < self.ttl_h) and cached["date"].max() >= last.isoformat():
                return cached
        df = self._download(first, last)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)
        return df

    def get_hourly_weather(self, day: date) -> pd.DataFrame:
        key = (day.year, day.month)
        if key not in self._months or day.isoformat() not in set(self._months[key]["date"]):
            self._months[key] = self.load_month(day.year, day.month)
        df = self._months[key]
        df = df[df["date"] == day.isoformat()].sort_values("hour")
        if list(df["hour"]) != list(range(24)):
            raise WeatherUnavailable(f"{day} 沒有完整的 Open-Meteo 逐時天氣（可查 {self.FIRST_DAY} 到今天之後 "
                                     f"{self.FORECAST_DAYS} 天）")
        return df[WEATHER_COLUMNS].reset_index(drop=True)


def full_months(days: list[date]) -> list[tuple[int, int]]:
    """每天都有資料的完整月份（整月分析要用）。"""
    have = set(days)
    months = sorted({(d.year, d.month) for d in days})
    return [(y, m) for y, m in months
            if all(date(y, m, d) in have for d in range(1, calendar.monthrange(y, m)[1] + 1))]


METER_COLUMNS = ["hour", "space_id", "appliance_id", "kwh"]
METER_DIR = ROOT / "data" / "meter"


class MeterSource(ABC):
    """用電資料來源的共同介面：模擬結果與未來的智慧插座、電表資料都轉成同一種格式。

    appliance_id 可以是單一家電（智慧插座），也可以是 "main"（總電表，只有全屋合計）。
    """

    @abstractmethod
    def get_hourly_consumption(self, day: date) -> pd.DataFrame:
        """回傳欄位 hour、space_id、appliance_id、kwh（每小時用電度數）。"""


class CsvMeterSource(MeterSource):
    """讀取智慧插座或電表匯出的 CSV（data/meter/*.csv）。

    CSV 欄位：timestamp（ISO 時間，例如 2026-07-20T13:15:00）、space_id、appliance_id，
    以及 kwh（該筆期間的用電量）或 power_w（瞬時功率）其中之一。
    取樣間隔不限（1 分鐘、15 分鐘、1 小時都可以），會自動彙整成每小時。
    power_w 以「該筆到下一筆的時間」積分成能量；最後一筆沿用前一個間隔。
    """

    def __init__(self, path: Path):
        self.path = path
        df = pd.read_csv(path, parse_dates=["timestamp"])
        missing = {"timestamp", "space_id", "appliance_id"} - set(df.columns)
        if missing or not ({"kwh", "power_w"} & set(df.columns)):
            raise ValueError(f"{path.name} 欄位不足：需要 timestamp、space_id、appliance_id，以及 kwh 或 power_w")
        if "kwh" not in df.columns:
            df = df.sort_values(["space_id", "appliance_id", "timestamp"])
            step_h = (df.groupby(["space_id", "appliance_id"])["timestamp"].diff(-1).dt.total_seconds().abs() / 3600)
            step_h = step_h.fillna(step_h.groupby([df["space_id"], df["appliance_id"]]).transform("median"))
            df["kwh"] = df["power_w"] * step_h / 1000
        self.data = df

    def available_dates(self) -> list[date]:
        return sorted(set(self.data["timestamp"].dt.date))

    def get_hourly_consumption(self, day: date) -> pd.DataFrame:
        df = self.data[self.data["timestamp"].dt.date == day]
        if df.empty:
            raise ValueError(f"{self.path.name} 沒有 {day} 的資料")
        df = df.assign(hour=df["timestamp"].dt.hour)
        return df.groupby(["hour", "space_id", "appliance_id"], as_index=False)["kwh"].sum()[METER_COLUMNS]


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
