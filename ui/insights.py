"""整月分析的每日資料與數據分析式建議（第 6a 批；只整理 core 的推演結果，不改演算法）。

daily_table()：把整月各模式每天的推演結果整理成一張長表格（折線圖、累積電費、溫度關係圖共用）。
month_insights()：從每日資料找出規律，例如「室外日均溫每升高 1°C，冷氣多用幾度電」、
「假日比平日多用多少」、「最熱的幾天占全月用電多少」，產生有數字根據的建議。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

BASELINE = "基準（一般習慣）"


def daily_table(scenarios: dict) -> pd.DataFrame:
    """{模式名稱: [每日 SimulationResult]} → 長表格：
    date、模式、day_type、kwh、cost（流動電費）、ac_kwh、peak_kwh、temp_mean_c、temp_max_c、cum_cost（累積電費）。"""
    rows = []
    for name, days in scenarios.items():
        for r in days:
            rows.append({
                "date": r.day, "模式": name, "day_type": r.day_type, "kwh": r.total_kwh, "cost": r.energy_cost,
                "ac_kwh": float(r.by_category()["冷氣"]), "peak_kwh": r.kwh_by_period()["尖峰"],
                "temp_mean_c": float(r.hourly["temp_c"].mean()), "temp_max_c": float(r.hourly["temp_c"].max()),
            })
    df = pd.DataFrame(rows).sort_values(["模式", "date"], kind="stable")
    df["cum_cost"] = df.groupby("模式", sort=False)["cost"].cumsum()
    return df.reset_index(drop=True)


@dataclass
class Insight:
    icon: str
    title: str
    detail: str

    def html(self) -> str:
        return f"<b>{self.title}</b>：{self.detail}"


def temperature_slope(df: pd.DataFrame) -> tuple[float, float, float] | None:
    """冷氣用電對室外日均溫的線性迴歸：回傳（斜率 度/°C, 截距, R²）；資料不足或溫度沒有變化時回傳 None。"""
    x, y = df["temp_mean_c"].to_numpy(float), df["ac_kwh"].to_numpy(float)
    if len(x) < 5 or np.ptp(x) < 1.0 or y.sum() <= 0:
        return None
    slope, intercept = np.polyfit(x, y, 1)
    pred = slope * x + intercept
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float(((y - pred) ** 2).sum()) / ss_tot if ss_tot else 0.0
    return float(slope), float(intercept), r2


def month_insights(daily: pd.DataFrame, mode: str, price: float) -> list[Insight]:
    """依整月每日資料產生建議。mode：要和基準比較的模式；price：平均每度電價（元），換算金額用。"""
    base = daily[daily["模式"] == BASELINE]
    out: list[Insight] = []
    if base.empty:
        return out
    total = float(base["kwh"].sum())

    # 1. 溫度敏感度：天氣越熱，冷氣用電增加多少
    fit = temperature_slope(base)
    if fit and fit[0] > 0.05:
        slope, _, r2 = fit
        hot = base["temp_mean_c"].quantile(0.75)
        out.append(Insight("🌡️", "天氣越熱、冷氣越耗電",
                           f"室外日均溫每升高 1°C，冷氣每天約多用 <b>{slope:.2f} 度</b>（約 {slope * price:.1f} 元，"
                           f"R² = {r2:.2f}）。日均溫 ≥ {hot:.1f}°C 的日子，建議提早關窗簾、出門前預冷，"
                           "或在這些天改用省電模式。"))

    # 2. 最耗電的日子集中在哪裡
    if len(base) >= 7:
        top = base.nlargest(max(3, len(base) // 10), "kwh")
        share = float(top["kwh"].sum()) / total if total else 0.0
        out.append(Insight("📅", f"用電最多的 {len(top)} 天占全月 {share * 100:.0f}%",
                           f"分別是 {'、'.join(f'{d:%m/%d}' for d in top['date'])}，平均日均溫 "
                           f"{top['temp_mean_c'].mean():.1f}°C（全月平均 {base['temp_mean_c'].mean():.1f}°C）。"
                           "這幾天的用電最值得注意。"))

    # 3. 平日與假日
    by_type = base.groupby("day_type")["kwh"].mean()
    if {"weekday", "holiday"} <= set(by_type.index) and by_type["weekday"] > 0:
        diff = by_type["holiday"] - by_type["weekday"]
        n_holiday = int((base["day_type"] == "holiday").sum())
        if abs(diff) >= 0.5:
            more = "多" if diff > 0 else "少"
            tip = ("假日全天在家、冷氣開得久，可把洗衣、烘衣集中在假日（全天離峰電價）。" if diff > 0
                   else "平日晚上用電集中在尖峰時段，可利用省電或舒眠模式降低夜間用電。")
            out.append(Insight("🏖️", f"假日每天比平日{more}用 {abs(diff):.1f} 度",
                               f"平日平均 {by_type['weekday']:.1f} 度、假日 {by_type['holiday']:.1f} 度"
                               f"（本月 {n_holiday} 天假日）。{tip}"))

    # 4. 尖峰占比
    peak_share = float(base["peak_kwh"].sum()) / total if total else 0.0
    if peak_share >= 0.3:
        out.append(Insight("⚡", f"一般習慣有 {peak_share * 100:.0f}% 的電用在尖峰時段",
                           "把熱水器、洗衣、烘衣移到離峰（平日 0–9 時、週末全天），是不影響舒適又最直接的省法。"))

    # 5. 所選模式在哪些日子省最多
    chosen = daily[daily["模式"] == mode]
    if not chosen.empty and mode != BASELINE:
        merged = base[["date", "cost", "temp_mean_c"]].merge(chosen[["date", "cost"]], on="date",
                                                             suffixes=("_base", "_mode"))
        merged["saving"] = merged["cost_base"] - merged["cost_mode"]
        saving = float(merged["saving"].sum())
        if saving > 0:
            corr = merged[["saving", "temp_mean_c"]].corr().iloc[0, 1] if merged["saving"].std() > 0 else 0.0
            when = ("越熱的日子省越多" if corr >= 0.5 else "越涼的日子省越多" if corr <= -0.5
                    else "每天省的金額差不多")
            out.append(Insight("💰", f"{mode}整月可省 {saving:.0f} 元流動電費",
                               f"平均每天 {saving / len(merged):.1f} 元，{when}（與日均溫相關係數 {corr:.2f}）。"))
    return out
