"""分析層：指標計算、模式比較、節省建議。

指標分四類：
- 用電：總用電、冷氣用電、碳排放（用電 × 電力排碳係數）
- 電費：流動電費、月電費（三種電價方案）
- 尖峰：尖峰時段用電占比、最大需量（最大小時平均 kW）、負載率（平均 ÷ 最大，越高越平穩）
- 舒適度：溫度範圍指標（optimizer.comfort_score）＋ ISO 7730 PMV / PPD
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from core.load_model import humidity_ratio, saturation_pressure_kpa
from core.optimizer import Optimizer
from core.simulator import ControlPlan, SimulationResult

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
BASELINE = "基準（一般習慣）"


# ---------- PMV / PPD（ISO 7730） ----------

def pmv_ppd(ta: float, tr: float, vel: float, rh: float, met: float, clo: float, wme: float = 0.0) -> tuple[float, float]:
    """預測平均投票 PMV 與預測不滿意百分比 PPD（ISO 7730:2005 附錄 D 演算法）。

    ta 空氣溫度 °C、tr 平均輻射溫度 °C、vel 風速 m/s、rh 相對濕度 %、met 代謝率、clo 衣著熱阻。
    PMV：−3 冷 … 0 中性 … +3 熱；ISO 7730 建議 |PMV| ≤ 0.5（PPD ≤ 10%）。
    """
    pa = rh * 10 * math.exp(16.6536 - 4030.183 / (ta + 235))   # 水蒸氣分壓 Pa
    icl = 0.155 * clo                                         # 衣著熱阻 m²K/W
    m, w = met * 58.15, wme * 58.15
    mw = m - w
    fcl = 1 + 1.29 * icl if icl <= 0.078 else 1.05 + 0.645 * icl
    hcf = 12.1 * math.sqrt(vel)
    taa, tra = ta + 273, tr + 273
    tcla = taa + (35.5 - ta) / (3.5 * icl + 0.1)
    p1 = icl * fcl
    p2, p3, p4 = p1 * 3.96, p1 * 100, p1 * taa
    p5 = 308.7 - 0.028 * mw + p2 * (tra / 100) ** 4
    xn, xf = tcla / 100, tcla / 50
    hc = hcf
    for _ in range(150):                                      # 迭代求衣著表面溫度
        if abs(xn - xf) <= 0.00015:
            break
        xf = (xf + xn) / 2
        hcn = 2.38 * abs(100 * xf - taa) ** 0.25
        hc = max(hcf, hcn)
        xn = (p5 + p4 * hc - p2 * xf ** 4) / (100 + p3 * hc)
    tcl = 100 * xn - 273
    hl1 = 3.05e-3 * (5733 - 6.99 * mw - pa)                   # 皮膚擴散散熱
    hl2 = 0.42 * (mw - 58.15) if mw > 58.15 else 0.0          # 排汗散熱
    hl3 = 1.7e-5 * m * (5867 - pa)                            # 呼吸潛熱
    hl4 = 0.0014 * m * (34 - ta)                              # 呼吸顯熱
    hl5 = 3.96 * fcl * (xn ** 4 - (tra / 100) ** 4)           # 輻射
    hl6 = fcl * hc * (tcl - ta)                               # 對流
    ts = 0.303 * math.exp(-0.036 * m) + 0.028
    pmv = ts * (mw - hl1 - hl2 - hl3 - hl4 - hl5 - hl6)
    ppd = 100 - 95 * math.exp(-0.03353 * pmv ** 4 - 0.2179 * pmv ** 2)
    return pmv, ppd


def indoor_rh(t_room: float, t_out: float, rh_out: float) -> float:
    """冷氣沒開時，室內絕對濕度約等於室外；換算成室溫下的相對濕度。"""
    w = humidity_ratio(t_out, rh_out)
    pw = w * 101.325 / (0.622 + w)
    return min(100.0, 100 * pw / saturation_pressure_kpa(t_room))


# ---------- 模型校正（ASHRAE Guideline 14） ----------

def calibration_metrics(measured, simulated, limits: dict, n_params: int = 1) -> dict[str, float | bool]:
    """模擬與實測的誤差指標。

    NMBE（%）= Σ(實測 − 模擬) / ((n − p) × 實測平均) × 100：整體偏高或偏低
    CV(RMSE)（%）= √(Σ(實測 − 模擬)² / (n − p)) / 實測平均 × 100：逐筆誤差大小
    limits：{"nmbe_pct": …, "cvrmse_pct": …}；兩者都在範圍內才算校正通過。
    """
    m = pd.Series(measured, dtype=float).reset_index(drop=True)
    s = pd.Series(simulated, dtype=float).reset_index(drop=True)
    if len(m) != len(s) or len(m) <= n_params:
        raise ValueError("實測與模擬的資料筆數必須相同，且多於參數數量")
    mean = m.mean()
    if mean == 0:
        raise ValueError("實測平均為 0，無法計算相對誤差")
    dof = len(m) - n_params
    nmbe = 100 * (m - s).sum() / (dof * mean)
    cvrmse = 100 * math.sqrt(((m - s) ** 2).sum() / dof) / mean
    return {"筆數": len(m), "實測合計(kWh)": m.sum(), "模擬合計(kWh)": s.sum(),
            "NMBE(%)": nmbe, "CV(RMSE)(%)": cvrmse,
            "通過": abs(nmbe) <= limits["nmbe_pct"] and cvrmse <= limits["cvrmse_pct"]}


def compare_with_meter(measured, simulated, days: list[date], limits: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """以實測資料的每個通道（總電表 main 或單一家電）比對模擬結果。
    回傳（逐時對照表, 各通道校正指標）。"""
    rows = []
    for day in days:
        meas = measured.get_hourly_consumption(day)
        sim = simulated.get_hourly_consumption(day)
        for (space_id, appliance_id), group in meas.groupby(["space_id", "appliance_id"]):
            if appliance_id == "main":
                sim_hourly = sim.groupby("hour")["kwh"].sum()
            else:
                part = sim[(sim["space_id"] == space_id) & (sim["appliance_id"] == appliance_id)]
                sim_hourly = part.groupby("hour")["kwh"].sum()
            meas_hourly = group.groupby("hour")["kwh"].sum()
            for h in range(24):
                rows.append({"date": day, "hour": h, "channel": f"{space_id}/{appliance_id}",
                             "measured_kwh": float(meas_hourly.get(h, 0.0)),
                             "simulated_kwh": float(sim_hourly.get(h, 0.0))})
    detail = pd.DataFrame(rows)
    summary = pd.DataFrame([
        {"通道": channel, **calibration_metrics(g["measured_kwh"], g["simulated_kwh"], limits)}
        for channel, g in detail.groupby("channel", sort=False)
    ])
    return detail, summary


def markdown_table(df: pd.DataFrame) -> str:
    """把 DataFrame 轉成 Markdown 表格（不需額外套件）。"""
    header = "| " + " | ".join(map(str, df.columns)) + " |"
    sep = "|" + "|".join("---" for _ in df.columns) + "|"
    body = ["| " + " | ".join(str(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join([header, sep, *body])


# ---------- 節省建議 ----------

@dataclass
class Recommendation:
    title: str
    detail: str
    monthly_kwh: float = 0.0     # 每月可省度數
    monthly_saving: float = 0.0  # 每月可省電費（元）
    note: str = ""               # 與其他建議的關係（例如「已包含在…」），避免重複相加

    def headline(self) -> str:
        if self.monthly_saving <= 0:
            return self.title
        kwh = f"（{self.monthly_kwh:.0f} 度）" if self.monthly_kwh >= 0.5 else "（用電量不變，省在電價）"
        return f"{self.title}：每月約省 {self.monthly_saving:.0f} 元{kwh}"

    def as_text(self) -> str:
        text = f"【{self.headline()}】\n  {self.detail}"
        return text + (f"\n  ※ {self.note}" if self.note else "")


class Analyzer:
    def __init__(self, optimizer: Optimizer | None = None, config_path: Path = CONFIG_DIR / "metrics.json"):
        self.opt = optimizer or Optimizer()
        self.sim = self.opt.sim
        with open(config_path, encoding="utf-8") as f:
            self.cfg = json.load(f)

    # ----- 單日指標 -----

    def pmv_stats(self, result: SimulationResult) -> dict[str, float]:
        """有冷氣空間中清醒的人：平均 PPD（%）與 |PMV| ≤ 0.5 的人·時比例（%）。"""
        p = self.cfg["pmv"]
        season, _ = self.opt.season(result.day)
        clo = p["clo"][season]
        ctx_all = self.opt.contexts(result.day_type, season)
        rh_ac = self.sim.load_model.params["infiltration"]["indoor_rh_pct"]
        out_t, out_rh = list(result.hourly["temp_c"]), list(result.hourly["rh_pct"])
        by_space = {sid: g.sort_values("hour") for sid, g in result.climate.groupby("space_id")}
        people = ppd_sum = ok = 0.0
        for space in self.opt.ac_spaces:
            awake = ctx_all[space.id].awake
            climate = by_space[space.id]              # 逐欄轉成列表再逐時讀（比 .loc 逐格讀快數十倍）
            temps, on, load = list(climate["room_temp_c"]), list(climate["ac_on"]), list(climate["load_total_w"])
            rhs = list(climate["room_rh_pct"]) if "room_rh_pct" in climate.columns else None
            for h in range(24):
                if awake[h] <= 0:
                    continue
                t = temps[h]
                cooling = on[h] > 0 and load[h] > 0
                if rhs is not None:                      # 模擬的室內濕度（水氣平衡，A4）
                    rh = rhs[h]
                else:
                    rh = rh_ac if cooling else indoor_rh(t, out_t[h], out_rh[h])
                vel = p["air_speed_m_s"]["ac_on" if cooling else "ac_off"]
                pmv, ppd = pmv_ppd(t, t, vel, rh, p["met_awake"], clo)
                people += awake[h]
                ppd_sum += awake[h] * ppd
                ok += awake[h] * (abs(pmv) <= p["comfort_limit"])
        return {"平均PPD(%)": ppd_sum / people if people else 0.0,
                "PMV舒適比例(%)": 100 * ok / people if people else 100.0}

    def day_metrics(self, result: SimulationResult) -> dict[str, float]:
        periods = result.kwh_by_period()
        hourly = result.hourly["kwh"]
        peak = periods["尖峰"] + periods["半尖峰"]
        return {
            "用電(kWh)": result.total_kwh,
            "流動電費(元)": result.energy_cost,
            "尖峰占比(%)": 100 * peak / result.total_kwh if result.total_kwh else 0.0,
            "最大需量(kW)": result.peak_demand_kw,
            "負載率(%)": 100 * hourly.mean() / hourly.max() if hourly.max() else 0.0,
            "冷氣用電(kWh)": float(result.by_category()["冷氣"]),
            "碳排放(kg)": result.total_kwh * self.cfg["emission"]["kg_co2e_per_kwh"],
            **self.opt.comfort_score(result),
            **self.pmv_stats(result),
        }

    def compare_day(self, day: date, plan_id: str = "tou_2") -> tuple[pd.DataFrame, dict]:
        """基準與四種模式的完整指標比較，附「相對基準」的變化。"""
        _, results = self.opt.compare(day, plan_id)
        rows = []
        for key, r in results.items():
            sim_result = r if isinstance(r, SimulationResult) else r.result
            name = BASELINE if key == "基準" else self.opt.modes[key].name
            rows.append({"模式": name, **self.day_metrics(sim_result)})
        table = pd.DataFrame(rows)
        base = table.iloc[0]
        table["電費節省(%)"] = 100 * (1 - table["流動電費(元)"] / base["流動電費(元)"]) if base["流動電費(元)"] else 0.0
        table["用電節省(%)"] = 100 * (1 - table["用電(kWh)"] / base["用電(kWh)"])
        return table, results

    # ----- 整月 -----

    def compare_month(self, year: int, month: int, plan_id: str = "tou_2") -> tuple[pd.DataFrame, dict]:
        table, scenarios = self.opt.compare_month(year, month, plan_id)
        plan_name = self.sim.tariff.plans[plan_id]["name"]
        extra = []
        for days in scenarios.values():
            pmv = [self.pmv_stats(r) for r in days]
            kwh = sum(r.total_kwh for r in days)
            extra.append({
                "最大需量(kW)": max(r.peak_demand_kw for r in days),
                "碳排放(kg)": kwh * self.cfg["emission"]["kg_co2e_per_kwh"],
                "平均PPD(%)": sum(s["平均PPD(%)"] for s in pmv) / len(pmv),
                "PMV舒適比例(%)": sum(s["PMV舒適比例(%)"] for s in pmv) / len(pmv),
            })
        table = pd.concat([table, pd.DataFrame(extra)], axis=1)
        base_bill = table.loc[0, f"{plan_name}(元)"]
        table["月電費節省(%)"] = 100 * (1 - table[f"{plan_name}(元)"] / base_bill)
        return table, scenarios

    # ----- 節省建議 -----

    def recommendations(self, year: int, month: int, plan_id: str, table: pd.DataFrame,
                        scenarios: dict) -> list[Recommendation]:
        tariff = self.sim.tariff
        plan_name = tariff.plans[plan_id]["name"]
        col = f"{plan_name}(元)"
        base = table.iloc[0]
        base_days = scenarios[BASELINE]
        recs: list[Recommendation] = []

        # 冷氣占比太低（冬季）時，冷氣相關建議沒有意義
        base_ac = sum(float(r.by_category()["冷氣"]) for r in base_days)
        ac_matters = base_ac >= 0.05 * base["月用電(kWh)"]

        # 1. 冷氣設定溫度每調高 1°C 的效益（只改設定溫度，其他習慣不變）
        if ac_matters:
            base_sp = self.opt.baseline_plan.default_setpoint
            warmer = self.sim.run_month(year, month, plan_id, ControlPlan(name="+1°C", default_setpoint=base_sp + 1))
            bill_warm = warmer.bills().set_index("plan_id").loc[plan_id, "月電費"]
            saved_kwh = base["月用電(kWh)"] - warmer.total_kwh
            recs.append(Recommendation(
                "冷氣調高 1°C",
                f"冷氣設定從 {base_sp:.0f}°C 調到 {base_sp + 1:.0f}°C，冷氣用電減少 "
                f"{100 * saved_kwh / base_ac:.0f}%。經濟部建議冷氣設定 26–28°C。",
                saved_kwh, base[col] - bill_warm,
                note="不想改用模式時，只做這一步的效果；已包含在「改用模式」的節省中。"))

        # 2. 推薦模式：舒適時間 ≥ 95% 的模式中，月電費最低者
        modes = table.iloc[1:]
        comfy = modes[modes["平均舒適時間比例(%)"] >= 95]
        best = (comfy if not comfy.empty else modes).sort_values(col).iloc[0]
        reason = ("在舒適時間 ≥ 95% 的模式中電費最低" if not comfy.empty
                  else "各模式都無法達到 95% 舒適時間（此季節室溫主要受天氣影響，系統沒有暖氣可調），選電費最低者")
        mode_rec = Recommendation(
            f"改用{best['模式']}",
            f"{reason}；舒適時間由 {base['平均舒適時間比例(%)']:.0f}% "
            f"變為 {best['平均舒適時間比例(%)']:.0f}%，PMV 舒適比例 {base['PMV舒適比例(%)']:.0f}% → "
            f"{best['PMV舒適比例(%)']:.0f}%。",
            base["月用電(kWh)"] - best["月用電(kWh)"], base[col] - best[col],
            note="包含冷氣設定最佳化與可移轉負載排程。" if ac_matters
            else "此季節冷氣幾乎不開，節省主要來自可移轉負載排程與照明調整。")
        recs.append(mode_rec)

        # 3. 電價方案：推薦模式下，三種方案哪個最便宜
        plan_cols = {pid: f"{p['name']}(元)" for pid, p in tariff.plans.items()}
        cheapest = min(plan_cols, key=lambda pid: best[plan_cols[pid]])
        if cheapest != plan_id:
            if tariff.plans[cheapest]["type"] == "progressive":
                why = ("冷氣多在晚間尖峰使用且無法移轉，時間電價的離峰優惠用不到。" if ac_matters else
                       "這個家庭的月用電落在累進電價的較低級距，平均單價比時間電價的尖峰電價低得多，"
                       "而晚間照明、炊煮等用電仍在尖峰時段。")
            else:
                why = "這個方案的時段劃分和費率更符合這個家庭的用電時間。"
            recs.append(Recommendation(
                f"改用{tariff.plans[cheapest]['name']}",
                f"以{best['模式']}的用電模式計算，{tariff.plans[cheapest]['name']}每月 "
                f"{best[plan_cols[cheapest]]:.0f} 元，比目前的{plan_name} {best[col]:.0f} 元便宜。原因：{why}",
                0.0, best[col] - best[plan_cols[cheapest]],
                note="以已改用推薦模式為前提，可與「改用模式」相加。"))
        else:
            recs.append(Recommendation(f"維持{plan_name}", "以推薦模式的用電計算，目前的電價方案已是最便宜。"))

        # 4. 可移轉負載移到離峰（只有時間電價有效）
        best_days = scenarios[best["模式"]]
        if tariff.plans[plan_id]["type"] == "time_of_use":
            shift_saving = (sum(r.cost_by_category()["熱水與洗烘"] for r in base_days)
                            - sum(r.cost_by_category()["熱水與洗烘"] for r in best_days))
            if shift_saving > 0:
                recs.append(Recommendation(
                    "熱水與洗衣移到離峰",
                    "電熱水器、洗衣機、烘衣機用定時器排在離峰時段（已計入熱水器提早燒的散熱損失），"
                    "並錯開運轉以降低最大需量。",
                    0.0, shift_saving, note=f"已包含在「改用{best['模式']}」的節省中。"))

        # 5. 耗電大戶的設備改善
        totals: dict[str, float] = {}
        for r in base_days:
            for aid, kwh in r.records.groupby("appliance_id")["kwh"].sum().items():
                totals[aid] = totals.get(aid, 0.0) + kwh
        avg_price = base[col] / base["月用電(kWh)"]
        advice = self.cfg["appliance_advice"]
        names = {a.id: a.name for _, a in self.sim.house.all_appliances()}
        for aid, kwh in sorted(totals.items(), key=lambda x: -x[1])[:3]:
            share = 100 * kwh / base["月用電(kWh)"]
            if aid in advice:
                saved = kwh * advice[aid]["saving_ratio"]
                recs.append(Recommendation(
                    f"耗電大戶：{names[aid]}（占 {share:.0f}%）", advice[aid]["text"] + "（設備投資，需評估回收年限）。",
                    saved, saved * avg_price, note="以基準用電估算；設備改善與行為改善可疊加，但比例會縮小。"))

        # 6. 夜間過冷提醒（只在冷氣是主要用電的季節）
        if base["平均舒適時間比例(%)"] < 80 and ac_matters:
            recs.append(Recommendation(
                "避免入睡後過冷",
                f"基準情境舒適時間只有 {base['平均舒適時間比例(%)']:.0f}%：入睡後代謝率下降，"
                "整夜維持低溫反而偏冷。可用舒眠模式讓設定溫度隨睡眠逐步調高、起床前回溫。"))

        # 0. 綜合建議放最前面：推薦模式 + 最便宜的電價方案，相對目前（基準 + 目前方案）的總節省
        total_bill = best[plan_cols[cheapest]]
        comfort_change = best["平均舒適時間比例(%)"] - base["平均舒適時間比例(%)"]
        if comfort_change >= 5:
            comfort_text = f"舒適時間同時由 {base['平均舒適時間比例(%)']:.0f}% 提升到 {best['平均舒適時間比例(%)']:.0f}%。"
        elif comfort_change > -5:
            comfort_text = "舒適度大致維持不變。"
        else:
            comfort_text = f"但舒適時間由 {base['平均舒適時間比例(%)']:.0f}% 降到 {best['平均舒適時間比例(%)']:.0f}%。"
        summary = Recommendation(
            "綜合建議",
            f"改用{best['模式']}"
            + (f"並換成{tariff.plans[cheapest]['name']}" if cheapest != plan_id else "")
            + f"：每月電費 {base[col]:.0f} → {total_bill:.0f} 元（−{100 * (1 - total_bill / base[col]):.0f}%），"
            f"碳排放減少 {base['碳排放(kg)'] - best['碳排放(kg)']:.0f} kg CO2e，{comfort_text}",
            base["月用電(kWh)"] - best["月用電(kWh)"], base[col] - total_bill,
            note="以下各項建議彼此有重疊，請勿直接相加。")
        return [summary] + sorted(recs, key=lambda r: -r.monthly_saving)

    # ----- 報告 -----

    def write_report(self, year: int, month: int, plan_id: str, path: Path,
                     chart: Path | None = None) -> tuple[pd.DataFrame, list[Recommendation]]:
        table, scenarios = self.compare_month(year, month, plan_id)
        recs = self.recommendations(year, month, plan_id, table, scenarios)
        path.write_text(self.report_markdown(year, month, plan_id, table, recs, chart.name if chart else None),
                        encoding="utf-8")
        return table, recs

    def report_markdown(self, year: int, month: int, plan_id: str, table: pd.DataFrame,
                        recs: list[Recommendation], chart_name: str | None = None) -> str:
        """整月分析報告（Markdown 文字），可存檔或在網頁上下載。"""
        plan_name = self.sim.tariff.plans[plan_id]["name"]
        lines = [f"# {year} 年 {month} 月 HEMS 模式比較與節省建議", "",
                 f"- 房型：{self.sim.house.name}，{self.sim.house.occupants} 人",
                 f"- 電價方案：{plan_name}（各模式依此方案最佳化）",
                 "- 天氣：中央氣象署臺中測站逐時觀測資料",
                 f"- 碳排係數：{self.cfg['emission']['kg_co2e_per_kwh']} kg CO2e/kWh（{self.cfg['emission']['source']}）",
                 "", "## 模式比較", "", markdown_table(table.round(1)), ""]
        if chart_name:
            lines += [f"![模式比較]({chart_name})", ""]
        lines += ["## 節省建議（依每月可省金額排序）", ""]
        lines += [f"{i}. **{r.headline()}**  \n   {r.detail}" + (f"  \n   ※ {r.note}" if r.note else "")
                  for i, r in enumerate(recs, 1)]
        return "\n".join(lines) + "\n"


# ---------- 命令列 ----------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="HEMS 指標、模式比較與節省建議")
    parser.add_argument("day", nargs="?", help="日期；省略時跑夏、冬代表日")
    parser.add_argument("--month", help="整月分析與節省建議，例如 2026-07（約 3 秒，多核心平行）")
    parser.add_argument("--plan", default="tou_2", help="電價方案：tou_2 / tou_3 / progressive")
    parser.add_argument("--calibrate", help="用電表／智慧插座 CSV 校正模型，例如 data/meter/demo_synthetic_2026-07.csv")
    args = parser.parse_args()

    pd.set_option("display.unicode.east_asian_width", True)
    pd.set_option("display.width", 250)
    out_dir = Path(__file__).resolve().parent.parent / "output"
    out_dir.mkdir(exist_ok=True)
    analyzer = Analyzer()
    from core.charts import plot_metric_panels

    if args.calibrate:
        from core.data_source import CsvMeterSource
        from core.simulator import SimulatedMeterSource
        measured = CsvMeterSource(Path(args.calibrate))
        days = measured.available_dates()
        limits = analyzer.cfg["calibration"]["hourly"]
        detail, summary = compare_with_meter(measured, SimulatedMeterSource(analyzer.sim), days, limits)
        print(f"\n=== 模型校正（ASHRAE Guideline 14，逐時：|NMBE| ≤ {limits['nmbe_pct']}%、"
              f"CV(RMSE) ≤ {limits['cvrmse_pct']}%）===")
        print(f"資料：{measured.path.name}，{days[0]} ～ {days[-1]}，共 {len(days)} 天")
        print(summary.round(2).to_string(index=False))
        detail.to_csv(out_dir / "calibration_detail.csv", index=False, encoding="utf-8-sig")
        print(f"\n逐時對照：{out_dir / 'calibration_detail.csv'}")
        return

    if args.month:
        year, month = map(int, args.month.split("-"))
        plan_name = analyzer.sim.tariff.plans[args.plan]["name"]
        chart = out_dir / f"compare_month_{args.month}.png"
        report = out_dir / f"report_{args.month}.md"
        table, recs = analyzer.write_report(year, month, args.plan, report, chart)
        plot_metric_panels(table, [
            (f"{plan_name}(元)", "月電費（元）", "{:.0f}"), ("月用電(kWh)", "月用電（kWh）", "{:.0f}"),
            ("碳排放(kg)", "碳排放（kg CO2e）", "{:.0f}"), ("最大需量(kW)", "最大需量（kW）", "{:.2f}"),
            ("平均舒適時間比例(%)", "舒適時間比例（%）", "{:.0f}"), ("平均PPD(%)", "平均 PPD（%，越低越好）", "{:.1f}"),
        ], f"{year} 年 {month} 月四種模式比較（{plan_name}）", chart)
        print(f"\n=== {args.month} 整月指標比較 ===")
        print(table.round(1).to_string(index=False))
        print("\n=== 節省建議 ===")
        for i, r in enumerate(recs, 1):
            print(f"{i}. {r.as_text()}")
        print(f"\n報告：{report}\n圖檔：{chart}")
        return

    if args.day:
        days = [date.fromisoformat(args.day)]
    else:
        from core.data_source import load_representative_days
        days = list(load_representative_days().values())
    for day in days:
        table, _ = analyzer.compare_day(day, args.plan)
        print(f"\n=== {day} 指標比較 ===")
        print(table.round(2).to_string(index=False))
        chart = out_dir / f"compare_{day.isoformat()}.png"
        plot_metric_panels(table, [
            ("流動電費(元)", "流動電費（元）", "{:.0f}"), ("用電(kWh)", "用電（kWh）", "{:.1f}"),
            ("尖峰占比(%)", "尖峰時段用電占比（%）", "{:.0f}"), ("最大需量(kW)", "最大需量（kW）", "{:.2f}"),
            ("舒適時間比例(%)", "舒適時間比例（%）", "{:.0f}"), ("平均PPD(%)", "平均 PPD（%，越低越好）", "{:.1f}"),
        ], f"{day} 四種模式指標比較", chart)
        table.to_csv(out_dir / f"compare_{day.isoformat()}.csv", index=False, encoding="utf-8-sig")
        print(f"圖檔：{chart}")


if __name__ == "__main__":
    main()
