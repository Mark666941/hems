"""台電住宅電價：判斷夏月／離峰日，取得某天每小時的時段與費率。"""
from __future__ import annotations

import json
import warnings
from datetime import date
from enum import Enum
from pathlib import Path

import pandas as pd

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class Period(Enum):
    PEAK = "peak"
    SEMI_PEAK = "semi_peak"
    OFF_PEAK = "off_peak"
    FLAT = "flat"   # 累進電價：不分時段

    @property
    def label(self) -> str:
        return {"peak": "尖峰", "semi_peak": "半尖峰", "off_peak": "離峰", "flat": "不分時段"}[self.value]


class Tariff:
    """讀取 tariff.json，提供電價查詢。"""

    def __init__(self, path: Path = CONFIG_DIR / "tariff.json"):
        with open(path, encoding="utf-8") as f:
            self.config = json.load(f)
        self.source = self.config["source"]
        self.plans = self.config["plans"]
        self._warned_years: set[int] = set()
        self._validate()

    def _validate(self) -> None:
        """確認每個時間電價方案的時段剛好涵蓋 0–24 時、不重疊。"""
        for pid, plan in self.plans.items():
            if plan["type"] != "time_of_use":
                continue
            for season in ("summer", "non_summer"):
                for day_type, rules in plan[season].items():
                    hours = sorted(h for r in rules for h in range(r["start"], r["end"]))
                    if hours != list(range(24)):
                        raise ValueError(f"{pid} {season} {day_type} 的時段沒有剛好涵蓋 24 小時")

    def plan_names(self) -> dict[str, str]:
        return {pid: p["name"] for pid, p in self.plans.items()}

    def is_summer(self, day: date) -> bool:
        return day.month in self.config["summer_months"]

    def is_off_peak_day(self, day: date) -> bool:
        """週六、週日或台電公告的離峰日。"""
        if day.weekday() >= 5:
            return True
        days = self.config["off_peak_days"]
        if day.strftime("%m-%d") in days["fixed"]:
            return True
        if str(day.year) not in days["by_year"] and day.year not in self._warned_years:
            # 農曆節日（春節、端午、中秋、清明）每年日期不同，缺少該年資料時只會判斷到國曆節日
            self._warned_years.add(day.year)
            warnings.warn(f"tariff.json 沒有 {day.year} 年的農曆離峰日，春節等節日會被當成平日", stacklevel=2)
        by_year = days["by_year"].get(str(day.year), {})
        return any(day.isoformat() in dates for dates in by_year.values())

    def marginal_progressive_rate(self, day: date, kwh_per_month: float | None = None) -> float:
        """累進電價的邊際電價：月用電落在哪個級距，每多用一度就付該級距的費率。"""
        plan = self.plans["progressive"]
        kwh = plan["marginal_estimate_kwh_per_month"] if kwh_per_month is None else kwh_per_month
        season = "summer" if self.is_summer(day) else "non_summer"
        for tier in plan["tiers"]:
            if tier["up_to"] is None or kwh <= tier["up_to"]:
                return tier[season]
        raise ValueError("累進電價級距設定錯誤")

    def hourly_prices(self, day: date, plan_id: str = "tou_2") -> pd.DataFrame:
        """回傳 24 列：hour、period（尖峰/半尖峰/離峰/不分時段）、price（元/度）。
        累進電價各時段相同，以邊際電價表示。"""
        plan = self.plans[plan_id]
        if plan["type"] == "progressive":
            rate = self.marginal_progressive_rate(day)
            return pd.DataFrame({"hour": range(24), "period": [Period.FLAT] * 24, "price": [rate] * 24})
        season = "summer" if self.is_summer(day) else "non_summer"
        day_type = "off_peak_day" if self.is_off_peak_day(day) else "weekday"

        rows = []
        for rule in plan[season][day_type]:
            for h in range(rule["start"], rule["end"]):
                rows.append({"hour": h, "period": Period(rule["period"]), "price": rule["rate"]})
        return pd.DataFrame(rows).sort_values("hour").reset_index(drop=True)

    def tou_monthly_bill(self, plan_id: str, energy_charge: float, kwh_per_month: float) -> dict[str, float]:
        """時間電價月電費（元）。energy_charge 為逐時「度數 × 當時費率」加總（流動電費）。
        另加基本電費，以及每月總度數超過門檻部分的加價。"""
        plan = self.plans[plan_id]
        surcharge = plan["surcharge"]
        extra = max(0.0, kwh_per_month - surcharge["over_kwh_per_month"]) * surcharge["add_per_kwh"]
        basic = plan["basic_fee_per_month"]
        return {"basic": basic, "energy": energy_charge, "surcharge": extra,
                "total": basic + energy_charge + extra}

    def progressive_bill(self, kwh_per_month: float, summer: bool) -> float:
        """累進電價的月電費（元）。"""
        season = "summer" if summer else "non_summer"
        bill, lower = 0.0, 0.0
        for tier in self.plans["progressive"]["tiers"]:
            upper = tier["up_to"] if tier["up_to"] is not None else float("inf")
            if kwh_per_month > lower:
                bill += (min(kwh_per_month, upper) - lower) * tier[season]
            lower = upper
        return bill
