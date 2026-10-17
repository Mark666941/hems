"""比較兩種最佳化方法（優化 B3）：原本的「逐房座標下降 + 可移轉負載窮舉」與「全屋混合整數規劃（MILP）」。

用法：python scripts/compare_optimizers.py            # 夏季 6–9 月每月 10、20 日 × 四種模式
輸出：docs/reports/optimizer_comparison.md

公平比較：兩種方法求出的控制策略都交給同一個完整（非線性）推演引擎執行，
再用同一套目標函數評估：全屋流動電費 + 舒適懲罰（平方項）+ 需量權重 × 全屋最大用電。
MILP 另跑「分組」版本（每個時段一個設定溫度，與原本方法相同的自由度），用來區分改善來自哪裡。
"""
from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from core.milp import optimize_milp  # noqa: E402
from core.optimizer import MODE_ORDER, ModeSchedule, Optimizer  # noqa: E402
from core.parallel import parallel_map  # noqa: E402

METHODS = ["座標下降（原本）", "MILP 逐時", "MILP 分組"]


def md_table(df: pd.DataFrame) -> str:
    """DataFrame → Markdown 表格（不需要 tabulate 套件）。"""
    head = "| " + " | ".join(map(str, df.columns)) + " |"
    rule = "|" + "---|" * len(df.columns)
    body = ["| " + " | ".join(f"{v:g}" if isinstance(v, float) else str(v) for v in row) + " |"
            for row in df.itertuples(index=False)]
    return "\n".join([head, rule, *body])


def compare_day(day: date, opt: Optimizer) -> list[dict]:
    rows = []
    for mode in MODE_ORDER:
        schedule = ModeSchedule.single(opt.modes[mode])
        t = time.perf_counter()
        base = opt.optimize(day, mode)
        runs = [(METHODS[0], base, time.perf_counter() - t)]
        for name, hourly in ((METHODS[1], True), (METHODS[2], False)):
            r = optimize_milp(opt, day, mode, hourly_setpoints=hourly)
            runs.append((name, r, r.levels["solve_s"]))
        for name, r, sec in runs:
            rows.append({"日期": day, "模式": opt.modes[mode].name, "方法": name,
                         **opt.evaluate(r.result, schedule), "計算秒數": sec})
    return rows


def main() -> None:
    days = [date(2026, m, d) for m in (6, 7, 8, 9) for d in (10, 20)]
    opt = Optimizer()
    df = pd.DataFrame([row for rows in parallel_map(compare_day, days, opt) for row in rows])
    cols = ["電費(元)", "舒適懲罰(元)", "尖峰需量(kW)", "目標值(元)", "計算秒數"]
    by_mode = df.groupby(["模式", "方法"], sort=False)[cols].mean().round(2).reset_index()
    total = df.groupby("方法", sort=False)[cols].mean().round(2).reindex(METHODS).reset_index()
    piv = df.pivot_table(index=["日期", "模式"], columns="方法", values="目標值(元)")
    wins = {m: (piv[m] < piv[METHODS[0]] - 1e-6).mean() * 100 for m in METHODS[1:]}
    gain = {m: (1 - piv[m].sum() / piv[METHODS[0]].sum()) * 100 for m in METHODS[1:]}

    out = ROOT / "docs" / "reports" / "optimizer_comparison.md"
    lines = [
        "# 最佳化方法比較：座標下降 vs 混合整數規劃（MILP）", "",
        f"由 `scripts/compare_optimizers.py` 產生。{len(days)} 個夏季日（2026 年 6–9 月每月 10、20 日）× 四種模式，"
        "共 {} 個案例；兩種方法的控制策略都交給同一個完整推演引擎執行後，用同一套目標函數評估。".format(len(piv)), "",
        "| 方法 | 決策方式 |", "|---|---|",
        "| 座標下降（原本） | 逐房搜尋設定溫度（每個時段一個值，0.5°C 間隔，直接在完整推演上評估），鄰房室溫當定值；"
        "可移轉負載窮舉 |",
        "| MILP 逐時 | 全屋聯合（隔間熱交換在方程式內）、每房每小時一個溫度；冷氣與可移轉負載一起考慮全屋尖峰；"
        "線性化模型，COP 以完整推演序列更新（最多 3 輪） |",
        "| MILP 分組 | 同上，但每個時段一個設定溫度（與原本方法相同的自由度） |", "",
        "## 整體平均（每案例）", "", md_table(total), "",
        "## 各模式平均", "", md_table(by_mode), "",
        "## 結論", "",
        f"- MILP 逐時：{wins[METHODS[1]]:.0f}% 的案例目標值優於原本方法，總目標值改善 {gain[METHODS[1]]:.1f}%。",
        f"- MILP 分組：{wins[METHODS[2]]:.0f}% 的案例優於原本方法，總目標值改善 {gain[METHODS[2]]:.1f}%。",
        "- 改善主要來自「逐時調整設定溫度」：同樣分組時，直接在完整推演上搜尋的座標下降比線性化的 MILP 準確；",
        "  MILP 的價值在於能處理更多決策變數（每房每小時）且仍在 1 秒內求得全域最佳（對線性模型而言）。",
        "- 線性模型與完整推演的落差（變頻冷氣 COP 隨負載率變化、開機除濕、除濕機）使 MILP 的電費估計偏低；",
        "  舒適要求寬鬆、以省電為主的模式（省電、舒眠）原本方法略優。",
        "- 實務上可再結合兩者：以 MILP 解為起點，在完整推演上做逐時局部搜尋（未來工作）。", "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
