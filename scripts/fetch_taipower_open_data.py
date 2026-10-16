"""下載並整理台電公開資料，作為用電模型的真實驗證資料（優化方案 B1a）。

用法：python scripts/fetch_taipower_open_data.py          # 下載最新資料並整理
      python scripts/fetch_taipower_open_data.py --offline # 只整理 data/public/raw/ 已下載的檔案

資料來源（政府資料開放平臺，政府資料開放授權條款第 1 版）：
1. 台灣電力公司住宅用戶每月平均用電度數及電費（全國）  https://data.gov.tw/dataset/17062
2. 台灣電力公司各縣市住宅、服務業及機關用電統計資料    https://data.gov.tw/dataset/29935

輸出（data/public/）：
- taipower_residential_avg.csv：month、kwh_per_household、kwh_per_household_last_year、
  bill_per_household、bill_per_household_last_year（全國住宅每戶每月平均）
- taichung_residential_monthly.csv：month、kwh_total（台中市住宅部門每月總售電量）
"""
from __future__ import annotations

import argparse
import ssl
import urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "public" / "raw"
OUT = ROOT / "data" / "public"
SOURCES = {
    "taipower_residential_avg.csv": "https://service.taipower.com.tw/data/opendata/apply/file/d007004/001.csv",
    "taipower_county_sales.csv": "https://service.taipower.com.tw/data/opendata/apply/file/d007012/001.csv",
}


def download() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT   # 部分政府網站憑證缺少 Subject Key Identifier
    for name, url in SOURCES.items():
        with urllib.request.urlopen(url, timeout=60, context=ctx) as resp:
            (RAW / name).write_bytes(resp.read())
        print(f"已下載 {name}")


def roc_to_month(value) -> str:
    """民國年月（例如 11507 或 11507.0）→ 西元 '2026-07'。"""
    v = int(float(value))
    return f"{v // 100 + 1911}-{v % 100:02d}"


def to_number(value) -> float:
    return float(str(value).replace(",", "").strip())


def clean_residential_avg() -> pd.DataFrame:
    df = pd.read_csv(RAW / "taipower_residential_avg.csv", encoding="utf-8-sig")
    out = pd.DataFrame({
        "month": df["日期(年/月)"].map(roc_to_month),
        "kwh_per_household": df["住宅本期平均用電度數(度/戶)"].map(to_number),
        "kwh_per_household_last_year": df["住宅去年同期平均用電度數(度/戶)"].map(to_number),
        "bill_per_household": df["住宅本期含稅電費(元/戶)"].map(to_number),
        "bill_per_household_last_year": df["住宅去年同期含稅電費(元/戶)"].map(to_number),
    })
    return out.sort_values("month").reset_index(drop=True)


def clean_taichung_residential() -> pd.DataFrame:
    df = pd.read_csv(RAW / "taipower_county_sales.csv", encoding="utf-8-sig").dropna(subset=["年月", "縣市"])
    df = df[(df["縣市"].isin(["台中市", "臺中市"])) & (df["用電性質"] == "住宅")]
    out = pd.DataFrame({"month": df["年月"].map(roc_to_month), "kwh_total": df["售電量(度)"].map(to_number)})
    return out.drop_duplicates("month").sort_values("month").reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="下載並整理台電公開用電資料")
    parser.add_argument("--offline", action="store_true", help="不下載，只整理已存在的原始檔")
    args = parser.parse_args()
    if not args.offline:
        download()
    avg, taichung = clean_residential_avg(), clean_taichung_residential()
    avg.to_csv(OUT / "taipower_residential_avg.csv", index=False, encoding="utf-8")
    taichung.to_csv(OUT / "taichung_residential_monthly.csv", index=False, encoding="utf-8")
    print(f"全國住宅每戶平均：{avg.month.iloc[0]} ～ {avg.month.iloc[-1]}，共 {len(avg)} 個月")
    print(f"台中市住宅總用電：{taichung.month.iloc[0]} ～ {taichung.month.iloc[-1]}，共 {len(taichung)} 個月")


if __name__ == "__main__":
    main()
