"""台電住宅電價：判斷夏月／離峰日，取得某天每小時的時段與費率。"""
from __future__ import annotations

import json
from datetime import date
from enum import Enum
from pathlib import Path

import pandas as pd

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class Period(Enum):
    PEAK = "peak"
    SEMI_PEAK = "semi_peak"
    OFF_PEAK = "off_peak"

    @property
    def label(self) -> str:
        return {"peak": "尖峰", "semi_peak": "半尖峰", "off_peak": "離峰"}[self.value]


class Tariff:
    """讀取 tariff.json，提供電價查詢。"""

    def __init__(self, path: Path = CONFIG_DIR / "tariff.json"):
        with open(path, encoding="utf-8") as f:
            self.config = json.load(f)
        self.source = self.config["source"]
        self.plans = self.config["plans"]
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
        by_year = days["by_year"].get(str(day.year), {})
        return any(day.isoformat() in dates for dates in by_year.values())

    def hourly_prices(self, day: date, plan_id: str = "tou_2") -> pd.DataFrame:
        """回傳 24 列：hour、period（尖峰/半尖峰/離峰）、price（元/度）。"""
        plan = self.plans[plan_id]
        if plan["type"] != "time_of_use":
            raise ValueError(f"{plan['name']} 不分時段，無法查詢每小時電價")
        season = "summer" if self.is_summer(day) else "non_summer"
        day_type = "off_peak_day" if self.is_off_peak_day(day) else "weekday"

        rows = []
        for rule in plan[season][day_type]:
            for h in range(rule["start"], rule["end"]):
                rows.append({"hour": h, "period": Period(rule["period"]), "price": rule["rate"]})
        return pd.DataFrame(rows).sort_values("hour").reset_index(drop=True)

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
