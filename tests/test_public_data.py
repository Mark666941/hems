"""B1a：台電公開資料整理結果（離線測試，只讀取已整理好的 CSV）。"""
from pathlib import Path

import pandas as pd
import pytest

PUBLIC = Path(__file__).resolve().parent.parent / "data" / "public"


def test_roc_month_conversion():
    import importlib.util
    spec = importlib.util.spec_from_file_location("fetch", PUBLIC.parent.parent / "scripts" / "fetch_taipower_open_data.py")
    fetch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fetch)
    assert fetch.roc_to_month(11507) == "2026-07"
    assert fetch.roc_to_month("10701.0") == "2018-01"
    assert fetch.to_number("636,471,432") == 636471432


def test_residential_average_covers_2026():
    df = pd.read_csv(PUBLIC / "taipower_residential_avg.csv")
    assert list(df.columns) == ["month", "kwh_per_household", "kwh_per_household_last_year",
                                "bill_per_household", "bill_per_household_last_year"]
    months = set(df["month"])
    assert {f"2026-{m:02d}" for m in range(1, 10)} <= months      # 與天氣資料同期
    assert df["kwh_per_household"].between(100, 800).all()          # 每戶每月合理範圍


def test_taichung_residential_monthly():
    df = pd.read_csv(PUBLIC / "taichung_residential_monthly.csv")
    assert df["month"].is_monotonic_increasing and df["month"].is_unique
    summer = df[df["month"] == "2025-08"]["kwh_total"].item()
    winter = df[df["month"] == "2025-02"]["kwh_total"].item()
    assert summer > winter * 1.2                                    # 夏季住宅用電明顯較高
    assert df["kwh_total"].min() > 1e8                              # 台中住宅每月至少 1 億度


@pytest.mark.parametrize("name", ["taipower_residential_avg.csv", "taipower_county_sales.csv"])
def test_raw_files_kept_for_reproducibility(name):
    assert (PUBLIC / "raw" / name).stat().st_size > 1000
