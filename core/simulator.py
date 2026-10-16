"""24 小時推演引擎：把房屋、作息、天氣、電價、負載模型串起來，算出全屋逐時用電。

流程：
    日期 → 天氣（data_source）、平日/假日與電價（tariff）
         → 每個空間：一般家電 + 冷氣（load_model.space_hourly）
         → 可移轉工作：洗衣、烘衣、燒熱水（依 ControlPlan 的開始時間）
         → 彙整成 SimulationResult（逐時、分空間、分類、電費、尖峰）

ControlPlan 是「怎麼控制家電」的描述。基準情境用一般習慣；
階段 6 的四種模式只要產生不同的 ControlPlan，推演引擎完全不用改。
"""
from __future__ import annotations

import calendar
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from core.data_source import CwaCsvWeatherSource, WeatherSource
from core.load_model import LoadModel, place_task, task_hourly
from core.models import House, LoadType, load_house
from core.schedule import Schedule, day_type_for
from core.tariff import Tariff

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# 圖表與報表用的用電分類（顏色固定跟著分類走）
CATEGORIES = ["冷氣", "熱水與洗烘", "其他家電", "必要負載"]


def categorize(appliance_id: str, load_type: LoadType) -> str:
    if appliance_id == "air_conditioner":
        return "冷氣"
    if load_type is LoadType.SHIFTABLE:
        return "熱水與洗烘"
    if load_type is LoadType.ESSENTIAL:
        return "必要負載"
    return "其他家電"


@dataclass
class ControlPlan:
    """控制策略。未指定的項目沿用預設（基準習慣）。

    setpoints：空間 id → 24 小時冷氣設定溫度（°C）
    ac_on：空間 id → 24 小時冷氣運轉比例（0–1）；未指定時「有人就開、涼爽時開窗」
    task_starts：家電 id → 可移轉工作的開始時間（時，可有小數）
    appliance_factors：空間 id → {家電 id → 24 小時調整係數}，例如照明調光、除濕機關閉
    """

    name: str = "基準"
    default_setpoint: float = 25.0
    setpoints: dict[str, list[float]] = field(default_factory=dict)
    ac_on: dict[str, list[float]] = field(default_factory=dict)
    task_starts: dict[str, float] = field(default_factory=dict)
    appliance_factors: dict[str, dict[str, list[float]]] = field(default_factory=dict)

    def setpoint_for(self, space_id: str) -> list[float]:
        return list(self.setpoints.get(space_id, [self.default_setpoint] * 24))


def load_baseline_plan(path: Path = CONFIG_DIR / "baseline.json") -> ControlPlan:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return ControlPlan(name=cfg["name"], default_setpoint=cfg["ac"]["setpoint_c"])


@dataclass
class SimulationResult:
    """一天的推演結果。"""

    day: date
    day_type: str
    plan_id: str            # 電價方案
    control_name: str       # 控制策略名稱
    records: pd.DataFrame   # 長表格：hour, space_id, space, appliance_id, appliance, load_type, category, kwh
    hourly: pd.DataFrame    # 24 列：hour, temp_c, rh_pct, period, period_label, price, kwh, cost
    climate: pd.DataFrame   # 各空間逐時室內狀態：hour, space_id, space, setpoint_c, ac_on, room_temp_c, load_total_w, unmet_w, cop
    tasks: list[dict]       # 可移轉工作與實際開始時間

    # ----- 彙整 -----

    @property
    def total_kwh(self) -> float:
        return float(self.records["kwh"].sum())

    @property
    def energy_cost(self) -> float:
        """當天流動電費（元），不含基本電費。累進電價以邊際電價估算，月電費請用 MonthResult.bills()。"""
        return float(self.hourly["cost"].sum())

    def kwh_by_period(self) -> dict[str, float]:
        """各電價時段用電。累進電價不分時段，全部計入「不分時段」。"""
        g = self.hourly.groupby("period_label")["kwh"].sum()
        return {label: float(g.get(label, 0.0)) for label in ("尖峰", "半尖峰", "離峰", "不分時段")}

    @property
    def peak_demand_kw(self) -> float:
        """最大小時平均需量（kW）。"""
        return float(self.hourly["kwh"].max())

    @property
    def peak_hour(self) -> int:
        return int(self.hourly["kwh"].idxmax())

    def by_space(self) -> pd.Series:
        return self.records.groupby("space", sort=False)["kwh"].sum()

    def by_category(self) -> pd.Series:
        return self.records.groupby("category")["kwh"].sum().reindex(CATEGORIES, fill_value=0.0)

    def by_appliance(self) -> pd.DataFrame:
        return (self.records.groupby(["space", "appliance"], sort=False)["kwh"].sum()
                .reset_index().query("kwh > 0"))

    def hourly_by_category(self) -> pd.DataFrame:
        """24 列 × 用電分類（kWh），畫堆疊圖用。"""
        return (self.records.pivot_table(index="hour", columns="category", values="kwh", aggfunc="sum")
                .reindex(index=range(24), columns=CATEGORIES, fill_value=0.0).fillna(0.0))

    def hourly_by_space(self) -> pd.DataFrame:
        order = list(dict.fromkeys(self.records["space"]))
        return (self.records.pivot_table(index="hour", columns="space", values="kwh", aggfunc="sum")
                .reindex(index=range(24), columns=order, fill_value=0.0).fillna(0.0))

    def summary(self) -> dict:
        periods = self.kwh_by_period()
        return {
            "日期": self.day.isoformat(),
            "日別": "平日" if self.day_type == "weekday" else "假日",
            "控制策略": self.control_name,
            "總用電(kWh)": round(self.total_kwh, 2),
            "流動電費(元)": round(self.energy_cost, 1),
            "尖峰用電(kWh)": round(periods["尖峰"], 2),
            "半尖峰用電(kWh)": round(periods["半尖峰"], 2),
            "離峰用電(kWh)": round(periods["離峰"], 2),
            "最大需量(kW)": round(self.peak_demand_kw, 2),
            "最大需量時段": f"{self.peak_hour:02d}:00",
            "冷氣用電(kWh)": round(float(self.by_category()["冷氣"]), 2),
            "冷氣能力不足(kWh)": round(float(self.climate["unmet_w"].sum()) / 1000, 3),
        }


class Simulator:
    """推演引擎。各元件都可以替換（例如換成感測器資料來源），預設讀取 config/ 的設定。"""

    def __init__(
        self,
        house: House | None = None,
        schedule: Schedule | None = None,
        load_model: LoadModel | None = None,
        weather: WeatherSource | None = None,
        tariff: Tariff | None = None,
    ):
        self.house = house or load_house()
        self.schedule = schedule or Schedule(self.house)
        self.load_model = load_model or LoadModel(self.house, self.schedule)
        self.weather = weather or CwaCsvWeatherSource()
        self.tariff = tariff or Tariff()

    def run(self, day: date, plan_id: str = "tou_2", control: ControlPlan | None = None) -> SimulationResult:
        control = control or load_baseline_plan()
        weather = self.weather.get_hourly_weather(day)
        day_type = day_type_for(day, self.tariff.is_off_peak_day)

        records, climate = [], []

        # 1. 各空間：一般家電 + 冷氣
        for space in self.house.spaces:
            df = self.load_model.space_hourly(
                space, weather, day_type,
                setpoint_c=control.setpoint_for(space.id),
                ac_on=control.ac_on.get(space.id),
                appliance_factors=control.appliance_factors.get(space.id),
            )
            for a in space.appliances:
                if a.id not in df.columns:
                    continue  # 可移轉家電另外處理
                category = categorize(a.id, a.load_type)
                for h, w in enumerate(df[a.id]):
                    records.append((h, space.id, space.name, a.id, a.name, a.load_type.label, category, w / 1000))
            if space.area_ping > 0:   # 公共設備沒有面積，不記錄室內狀態
                part = df[["hour", "setpoint_c", "ac_on", "room_temp_c", "load_total_w", "unmet_w", "cop"]].copy()
                part.insert(1, "space_id", space.id)
                part.insert(2, "space", space.name)
                climate.append(part)

        # 2. 可移轉工作
        tasks = []
        for task in self.load_model.shiftable_tasks(weather, day_type):
            t = place_task(task, control.task_starts.get(task["appliance"]))
            tasks.append(t)
            space = self.house.get_space(t["space"])
            appliance = space.get_appliance(t["appliance"])
            for h, w in enumerate(task_hourly(t, t["start"])):
                if w:
                    records.append((h, space.id, space.name, appliance.id, appliance.name,
                                    appliance.load_type.label, categorize(appliance.id, appliance.load_type), w / 1000))

        records_df = pd.DataFrame(records, columns=[
            "hour", "space_id", "space", "appliance_id", "appliance", "load_type", "category", "kwh"])
        # 同一台家電同一小時可能有多筆（例如洗衣機一天兩次），合併
        records_df = (records_df.groupby(["hour", "space_id", "space", "appliance_id", "appliance",
                                          "load_type", "category"], sort=False, as_index=False)["kwh"].sum())

        hourly = self._price_hourly(records_df, weather, day, plan_id)
        climate_df = pd.concat(climate, ignore_index=True) if climate else pd.DataFrame()
        return SimulationResult(day, day_type, plan_id, control.name, records_df, hourly, climate_df, tasks)

    def _price_hourly(self, records: pd.DataFrame, weather: pd.DataFrame, day: date, plan_id: str) -> pd.DataFrame:
        kwh = records.groupby("hour")["kwh"].sum().reindex(range(24), fill_value=0.0)
        prices = self.tariff.hourly_prices(day, plan_id)
        hourly = weather[["hour", "temp_c", "rh_pct"]].merge(prices, on="hour")
        hourly["period_label"] = hourly["period"].map(lambda p: p.label)
        hourly["kwh"] = kwh.values
        hourly["cost"] = hourly["kwh"] * hourly["price"]
        return hourly

    def reprice(self, result: SimulationResult, plan_id: str) -> SimulationResult:
        """同樣的用電，換成另一個電價方案計價（用電行為不變時，不必重新推演）。"""
        weather = result.hourly[["hour", "temp_c", "rh_pct"]]
        hourly = self._price_hourly(result.records, weather, result.day, plan_id)
        return SimulationResult(result.day, result.day_type, plan_id, result.control_name,
                                result.records, hourly, result.climate, result.tasks)

    # ----- 多日推演 -----

    def run_month(self, year: int, month: int, plan_id: str = "tou_2",
                  control: ControlPlan | None = None) -> "MonthResult":
        days = [date(year, month, d) for d in range(1, calendar.monthrange(year, month)[1] + 1)]
        return MonthResult(self, [self.run(d, plan_id, control) for d in days])


@dataclass
class MonthResult:
    """整月推演：每日結果與月電費（三種電價方案比較）。"""

    simulator: Simulator
    days: list[SimulationResult]

    @property
    def total_kwh(self) -> float:
        return sum(r.total_kwh for r in self.days)

    @property
    def is_summer(self) -> bool:
        return self.simulator.tariff.is_summer(self.days[0].day)

    def daily(self) -> pd.DataFrame:
        rows = []
        for r in self.days:
            periods = r.kwh_by_period()
            cats = r.by_category()
            weather = r.hourly
            rows.append({
                "date": r.day, "day_type": r.day_type,
                "temp_mean_c": weather["temp_c"].mean(), "temp_max_c": weather["temp_c"].max(),
                "kwh": r.total_kwh, "peak_kwh": periods["尖峰"], "semi_peak_kwh": periods["半尖峰"],
                "off_peak_kwh": periods["離峰"], "ac_kwh": cats["冷氣"],
                "peak_demand_kw": r.peak_demand_kw,
            })
        return pd.DataFrame(rows)

    def bills(self) -> pd.DataFrame:
        """同一個月用電，在三種電價方案下的月電費。"""
        tariff = self.simulator.tariff
        rows = []
        for plan_id, plan in tariff.plans.items():
            if plan["type"] == "time_of_use":
                energy = sum(self.simulator.reprice(r, plan_id).energy_cost for r in self.days)
                bill = tariff.tou_monthly_bill(plan_id, energy, self.total_kwh)
            else:
                total = tariff.progressive_bill(self.total_kwh, self.is_summer)
                bill = {"basic": 0.0, "energy": total, "surcharge": 0.0, "total": total}
            rows.append({"plan_id": plan_id, "方案": plan["name"], "基本電費": bill["basic"],
                         "流動電費": bill["energy"], "超量加價": bill["surcharge"], "月電費": bill["total"],
                         "平均每度": bill["total"] / self.total_kwh})
        return pd.DataFrame(rows).sort_values("月電費").reset_index(drop=True)


# ---------- 命令列 ----------

def _print_day(result: SimulationResult, tariff: Tariff) -> None:
    names = {"weekday": "平日", "holiday": "假日"}
    print(f"\n{'=' * 70}\n{result.day}（{names[result.day_type]}）｜{result.control_name}｜"
          f"{tariff.plans[result.plan_id]['name']}\n{'=' * 70}")

    table = result.hourly_by_category().copy()
    table.insert(0, "時段", result.hourly["period_label"].values)
    table.insert(0, "室外°C", result.hourly["temp_c"].values)
    table["合計kWh"] = result.hourly["kwh"].values
    table["電費元"] = result.hourly["cost"].values
    table.index = [f"{h:02d}:00" for h in range(24)]
    print(table.round(2).to_string())

    print("\n各空間用電（kWh）：")
    for space, kwh in result.by_space().items():
        share = kwh / result.total_kwh * 100
        print(f"  {space:<6}{kwh:7.2f}  {share:5.1f}%")

    print("\n各家電用電（kWh，前 10 名）：")
    top = result.by_appliance().sort_values("kwh", ascending=False).head(10)
    for row in top.itertuples():
        print(f"  {row.space}・{row.appliance:<6}{row.kwh:7.2f}")

    print("\n可移轉工作：")
    for t in result.tasks:
        print(f"  {t['name']:<6} {t['start']:05.2f} 時開始，運轉 {t['duration_h']:.2f} h，{t['energy_kwh']:.2f} kWh")

    print("\n摘要：")
    for k, v in result.summary().items():
        print(f"  {k}：{v}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="HEMS 24 小時推演（基準情境）")
    parser.add_argument("day", nargs="?", help="日期，例如 2026-07-20；省略時跑夏、冬代表日")
    parser.add_argument("--month", help="整月推演，例如 2026-07")
    parser.add_argument("--plan", default="tou_2", help="電價方案：tou_2 / tou_3（預設 tou_2）")
    parser.add_argument("--no-plot", action="store_true", help="不輸出圖檔")
    args = parser.parse_args()

    pd.set_option("display.unicode.east_asian_width", True)
    pd.set_option("display.width", 200)
    out_dir = Path(__file__).resolve().parent.parent / "output"
    out_dir.mkdir(exist_ok=True)
    sim = Simulator()

    if args.month:
        year, month = map(int, args.month.split("-"))
        result = sim.run_month(year, month, args.plan)
        daily = result.daily()
        print(f"\n=== {args.month} 整月推演（基準情境）===")
        show = daily.copy()
        show["day_type"] = show["day_type"].map({"weekday": "平日", "holiday": "假日"})
        print(show.round(2).to_string(index=False))
        print(f"\n月用電 {result.total_kwh:.1f} kWh，日平均 {daily.kwh.mean():.2f} kWh"
              f"（平日 {daily[daily.day_type == 'weekday'].kwh.mean():.2f}、"
              f"假日 {daily[daily.day_type == 'holiday'].kwh.mean():.2f}）")
        print("\n三種電價方案月電費比較：")
        print(result.bills().drop(columns="plan_id").round(1).to_string(index=False))
        daily.to_csv(out_dir / f"month_{args.month}.csv", index=False, encoding="utf-8-sig")
        if not args.no_plot:
            from core.charts import plot_month
            path = plot_month(result, out_dir / f"month_{args.month}.png")
            print(f"\n圖檔：{path}")
        return

    if args.day:
        days = [date.fromisoformat(args.day)]
    else:
        from core.data_source import load_representative_days
        days = list(load_representative_days().values())

    for day in days:
        result = sim.run(day, args.plan)
        _print_day(result, sim.tariff)
        stem = f"day_{day.isoformat()}"
        result.records.to_csv(out_dir / f"{stem}_records.csv", index=False, encoding="utf-8-sig")
        result.hourly.drop(columns="period").to_csv(out_dir / f"{stem}_hourly.csv", index=False, encoding="utf-8-sig")
        if not args.no_plot:
            from core.charts import plot_day
            print(f"\n圖檔：{plot_day(result, out_dir / f'{stem}.png')}")
    print(f"\nCSV 已輸出至 {out_dir}")


if __name__ == "__main__":
    main()
