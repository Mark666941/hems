"""從中央氣象署 CODiS 下載測站逐時觀測資料，存成 CSV。

用法（在專案根目錄執行）：
    python scripts/fetch_cwa_weather.py 2026-07
    python scripts/fetch_cwa_weather.py 2026-01 --station 467490

資料來源：中央氣象署 氣候觀測資料查詢服務 CODiS
    https://codis.cwa.gov.tw/StationData
預設測站：467490 臺中（局屬氣象站）

時間對齊：CODiS 的 01:00 觀測值代表 00:00–01:00 這一小時結束時的量測，
本程式把它對應到「第 0 小時」；24:00（CODiS 記為 23:59）對應到「第 23 小時」。
"""
from __future__ import annotations

import argparse
import calendar
import json
import ssl
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

API_URL = "https://codis.cwa.gov.tw/api/station"
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "weather"


def _ssl_context() -> ssl.SSLContext:
    """氣象署憑證缺少 Subject Key Identifier，Python 3.13+ 的嚴格模式會拒絕。
    這裡只關閉 X.509 嚴格檢查，憑證鏈與網域驗證仍然照常進行。"""
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def fetch_day(station: str, day: date) -> list[dict]:
    """下載某測站某一天的 24 筆逐時資料。"""
    d = day.isoformat()
    form = {
        "date": f"{d}T00:00:00.000+08:00",
        "type": "report_date",
        "stn_ID": station,
        "stn_type": "cwb",
        "more": "",
        "start": f"{d}T00:00:00",
        "end": f"{d}T23:59:59",
    }
    req = urllib.request.Request(API_URL, data=urllib.parse.urlencode(form).encode())
    with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as resp:
        payload = json.load(resp)
    if payload.get("code") != 200 or not payload.get("data"):
        raise RuntimeError(f"{d} 下載失敗：{payload.get('message')}")
    return payload["data"][0]["dts"]


TRACE_PRECIP = -9.8  # CODiS 以 -9.8 表示微量降雨（T，不足 0.1 mm）


def _value(record: dict, key: str, sub: str) -> float | None:
    v = (record.get(key) or {}).get(sub)
    if v is None:
        return None
    if key == "Precipitation" and v == TRACE_PRECIP:
        return 0.0
    # 其餘負的特殊值（如 -99.x）代表儀器故障或缺測；只有氣溫可能是真的負值
    if v < -90 or (v < 0 and key != "AirTemperature"):
        return None
    return float(v)


def parse_records(records: list[dict]) -> pd.DataFrame:
    rows = []
    for r in records:
        obs_time = datetime.fromisoformat(r["DataTime"])
        # CODiS 把 24:00 記成 23:59，先進位到整點
        if obs_time.minute:
            obs_time = obs_time.replace(minute=0) + timedelta(hours=1)
        start = obs_time - timedelta(hours=1)  # 這筆資料代表的小時起點
        rows.append({
            "date": start.date().isoformat(),
            "hour": start.hour,
            "temp_c": _value(r, "AirTemperature", "Instantaneous"),
            "rh_pct": _value(r, "RelativeHumidity", "Instantaneous"),
            "solar_mj_m2": _value(r, "GlobalSolarRadiation", "Accumulation"),
            "precip_mm": _value(r, "Precipitation", "Accumulation"),
            "wind_ms": _value(r, "WindSpeed", "Mean"),
        })
    return pd.DataFrame(rows)


def fetch_month(station: str, year: int, month: int) -> pd.DataFrame:
    days = calendar.monthrange(year, month)[1]
    frames = []
    for d in range(1, days + 1):
        frames.append(parse_records(fetch_day(station, date(year, month, d))))
        time.sleep(0.5)  # 避免對伺服器造成負擔
    df = pd.concat(frames, ignore_index=True)
    # 少量缺測用前後小時線性內插補齊，並回報補了幾筆（資料品質可追溯）
    value_cols = ["temp_c", "rh_pct", "solar_mj_m2", "precip_mm", "wind_ms"]
    missing = df[value_cols].isna().sum()
    if missing.any():
        print("內插補值筆數：" + "、".join(f"{c} {n}" for c, n in missing.items() if n))
    df[value_cols] = df[value_cols].interpolate(limit_direction="both")
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="下載 CWA 測站逐時資料")
    parser.add_argument("month", help="年月，例如 2026-07")
    parser.add_argument("--station", default="467490", help="測站代碼（預設 467490 臺中）")
    args = parser.parse_args()

    year, month = map(int, args.month.split("-"))
    df = fetch_month(args.station, year, month)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / f"{args.station}_{year}-{month:02d}.csv"
    df.to_csv(out, index=False, encoding="utf-8")
    print(f"已儲存 {len(df)} 筆 → {out}")


if __name__ == "__main__":
    main()
