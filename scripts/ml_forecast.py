"""機器學習用電預測：物理模擬 + 梯度提升樹修正殘差的混合模型（優化方案 B1b）。

用法：python scripts/ml_forecast.py      # 推演物理特徵（約 15 秒）、交叉驗證三種模型，輸出報告與圖

目標：全國住宅每戶每月平均用電（data/public/taipower_residential_avg.csv，2018-01 ～ 2026-09，B1a 整理）。
三種模型（都只用「預測當下已知」的資訊）：
1. 純物理：本專案的物理模擬（標配房型、一般習慣）算出每個月的用電，再用線性迴歸換算到「全國平均每戶」的量級。
   特徵為當月與上月的模擬用電——台電住宅多為兩個月抄表一次，月統計有時間落差（見 data/public/README.md）。
2. 純 ML：梯度提升樹（scikit-learn GradientBoostingRegressor），特徵為月份（sin/cos 週期編碼）、時間趨勢、去年同月用電。
3. 混合：先用純物理預測，再用梯度提升樹學習「實際 − 物理」的殘差（特徵＝純 ML 的特徵 + 物理模擬值）。
另列「去年同月」作為最簡單的對照。

驗證：時間序列交叉驗證（TimeSeriesSplit，5 折、每折預測接下來 12 個月），只用過去訓練、預測未來，
不會偷看未來資料；指標為 MAE（度）與 MAPE（%）。

限制：天氣資料只有 2026 年 1–9 月，物理特徵是以 2026 年天氣推演的「各月份典型值」（10–12 月用最接近月份的天氣），
所以物理模型提供的是季節形狀，不含逐年天氣差異。之後補齊歷年天氣，物理特徵就能反映每年的冷熱。
"""
from __future__ import annotations

import calendar
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import TimeSeriesSplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TARGET_CSV = ROOT / "data" / "public" / "taipower_residential_avg.csv"
REPORT = ROOT / "docs" / "reports" / "ml_forecast.md"
FIGURE = ROOT / "docs" / "images" / "ml_forecast.png"
MODELS = ["去年同月", "純物理", "純 ML", "混合（物理＋ML 殘差）"]
ML_FEATURES = ["month_sin", "month_cos", "t", "last_year"]
PHYS_FEATURES = ["phys", "phys_prev"]


# ---------- 資料 ----------

def load_target(path: Path = TARGET_CSV) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["year"] = df["month"].str[:4].astype(int)
    df["m"] = df["month"].str[5:7].astype(int)
    return df.rename(columns={"kwh_per_household": "kwh", "kwh_per_household_last_year": "last_year"})


def physics_daily_by_month(analyzer=None) -> dict[int, float]:
    """以物理模擬（一般習慣）算出 1–12 月每天平均用電（kWh）。沒有天氣資料的月份用最接近月份代替。"""
    from core.data_source import CwaCsvWeatherSource
    from ui.settings import build_analyzer, default_settings
    from ui.usage import source_month

    analyzer = analyzer or build_analyzer(default_settings())
    dates = CwaCsvWeatherSource().available_dates()
    months = sorted({(d.year, d.month) for d in dates})
    cache: dict[tuple[int, int], float] = {}
    out = {}
    for m in range(1, 13):
        src = source_month(2026, m, months, analyzer.sim.tariff)
        if src not in cache:
            days = [d for d in dates if (d.year, d.month) == src]
            cache[src] = float(np.mean([analyzer.sim.run(d, "tou_2", analyzer.opt.baseline_plan).total_kwh
                                        for d in days]))
        out[m] = cache[src]
    return out


def build_features(target: pd.DataFrame, phys_daily: dict[int, float]) -> pd.DataFrame:
    """每列一個月：目標值與特徵。物理特徵＝該月天數 × 每天平均模擬用電（當月與上月）。"""
    df = target.copy()
    df["t"] = np.arange(len(df), dtype=float)
    df["month_sin"] = np.sin(2 * np.pi * df["m"] / 12)
    df["month_cos"] = np.cos(2 * np.pi * df["m"] / 12)
    df["phys"] = [phys_daily[m] * calendar.monthrange(y, m)[1] for y, m in zip(df["year"], df["m"])]
    prev = [(y, m - 1) if m > 1 else (y - 1, 12) for y, m in zip(df["year"], df["m"])]
    df["phys_prev"] = [phys_daily[m] * calendar.monthrange(y, m)[1] for y, m in prev]
    return df


# ---------- 模型 ----------

def _gbr() -> GradientBoostingRegressor:
    return GradientBoostingRegressor(n_estimators=300, max_depth=2, learning_rate=0.05, subsample=0.8,
                                     random_state=0)


def fit_predict(train: pd.DataFrame, test: pd.DataFrame) -> dict[str, np.ndarray]:
    """用 train 訓練、預測 test，回傳 {模型名稱: 預測值}。"""
    y = train["kwh"].to_numpy(float)
    preds = {"去年同月": test["last_year"].to_numpy(float)}

    phys = LinearRegression().fit(train[PHYS_FEATURES], y)
    preds["純物理"] = phys.predict(test[PHYS_FEATURES])

    preds["純 ML"] = _gbr().fit(train[ML_FEATURES], y).predict(test[ML_FEATURES])

    residual = y - phys.predict(train[PHYS_FEATURES])
    hybrid_features = ML_FEATURES + PHYS_FEATURES
    correction = _gbr().fit(train[hybrid_features], residual)
    preds["混合（物理＋ML 殘差）"] = preds["純物理"] + correction.predict(test[hybrid_features])
    return preds


def cross_validate(df: pd.DataFrame, n_splits: int = 5, test_size: int = 12) -> tuple[pd.DataFrame, pd.DataFrame]:
    """時間序列交叉驗證。回傳（各折各模型的 MAE、MAPE, 每個測試月份的預測值）。"""
    rows, predictions = [], []
    for fold, (tr, te) in enumerate(TimeSeriesSplit(n_splits=n_splits, test_size=test_size).split(df), start=1):
        train, test = df.iloc[tr], df.iloc[te]
        actual = test["kwh"].to_numpy(float)
        for name, pred in fit_predict(train, test).items():
            err = pred - actual
            rows.append({"折": fold, "測試期間": f"{test['month'].iloc[0]}～{test['month'].iloc[-1]}", "模型": name,
                         "MAE(度)": float(np.abs(err).mean()), "MAPE(%)": float(100 * np.abs(err / actual).mean())})
            predictions.append(pd.DataFrame({"month": test["month"].to_numpy(), "模型": name, "預測": pred,
                                             "實際": actual}))
    return pd.DataFrame(rows), pd.concat(predictions, ignore_index=True)


def summarize(scores: pd.DataFrame) -> pd.DataFrame:
    """各模型的平均 MAE、MAPE（依 MAPE 由小到大）。"""
    out = scores.groupby("模型")[["MAE(度)", "MAPE(%)"]].mean().reindex(MODELS)
    return out.sort_values("MAPE(%)").reset_index()


# ---------- 輸出 ----------

def plot(predictions: pd.DataFrame, target: pd.DataFrame, path: Path = FIGURE) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    import core.charts  # noqa: F401  套用中文字型與圖表樣式

    fig, ax = plt.subplots(figsize=(11, 4.6))
    x = pd.to_datetime(target["month"])
    ax.plot(x, target["kwh"], color="#1f1f1f", lw=2.2, label="實際（全國每戶平均）")
    colors = {"去年同月": "#b0aea5", "純物理": "#eda100", "純 ML": "#2a78d6", "混合（物理＋ML 殘差）": "#eb6834"}
    for name in MODELS:
        p = predictions[predictions["模型"] == name]
        ax.plot(pd.to_datetime(p["month"]), p["預測"], color=colors[name], lw=1.4,
                ls="--" if name == "去年同月" else "-", label=f"{name}（交叉驗證預測）")
    ax.set_ylabel("每戶每月用電（度）")
    ax.set_title("用電預測模型比較：時間序列交叉驗證（每折預測未來 12 個月）", loc="left", fontsize=12, pad=30)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncols=5, frameon=False, fontsize=8.5)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def report_markdown(summary: pd.DataFrame, scores: pd.DataFrame, phys_daily: dict[int, float]) -> str:
    best = summary.iloc[0]
    phys_row = summary.set_index("模型").loc["純物理"]
    lines = [
        "# 用電預測模型比較（優化方案 B1b）",
        "",
        f"產生日期：{date.today()}｜由 `python scripts/ml_forecast.py` 產生｜目標：全國住宅每戶每月平均用電（2018-01 起）",
        "",
        "## 結果（時間序列交叉驗證 5 折，每折預測未來 12 個月的平均）",
        "",
        "| 模型 | MAE（度） | MAPE（%） |",
        "|---|---|---|",
        *[f"| {r['模型']} | {r['MAE(度)']:.1f} | {r['MAPE(%)']:.1f} |" for _, r in summary.iterrows()],
        "",
        f"- 最準的是 **{best['模型']}**（MAPE {best['MAPE(%)']:.1f}%）；純物理 MAPE {phys_row['MAPE(%)']:.1f}%。",
        "- 純物理只用模擬的季節形狀（線性換算量級），不知道逐年的變化；ML 能學到趨勢與去年同月的水準，"
        "混合模型則讓 ML 只修正物理模型「沒解釋到」的部分。",
        "",
        "![模型比較](../images/ml_forecast.png)",
        "",
        "## 各折明細",
        "",
        "| 折 | 測試期間 | 模型 | MAE（度） | MAPE（%） |",
        "|---|---|---|---|---|",
        *[f"| {r['折']} | {r['測試期間']} | {r['模型']} | {r['MAE(度)']:.1f} | {r['MAPE(%)']:.1f} |"
          for _, r in scores.iterrows()],
        "",
        "## 物理特徵：標配房型每天平均模擬用電（一般習慣，度）",
        "",
        "| " + " | ".join(f"{m} 月" for m in range(1, 13)) + " |",
        "|" + "---|" * 12,
        "| " + " | ".join(f"{phys_daily[m]:.1f}" for m in range(1, 13)) + " |",
        "",
        "## 方法與限制",
        "",
        "- 特徵只用預測當下已知的資訊：月份（sin/cos 週期編碼）、時間趨勢、去年同月用電（台電資料內附）、物理模擬值。",
        "- 物理特徵包含當月與上月：台電住宅多為兩個月抄表一次，月統計反映部分前一個月的用電。",
        "- 天氣資料只有 2026 年 1–9 月，物理特徵是各月份的典型值（10–12 月用最接近月份的天氣），不含逐年天氣差異；"
        "補齊歷年天氣後可再提升物理與混合模型。",
        "- 全國平均不是同一戶的資料，這裡驗證的是「模型能不能抓到真實住宅用電的季節與趨勢」，不是單一家庭的準確度。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    print("推演物理特徵（1–12 月）…")
    phys_daily = physics_daily_by_month()
    target = load_target()
    df = build_features(target, phys_daily)
    scores, predictions = cross_validate(df)
    summary = summarize(scores)
    print(summary.round(2).to_string(index=False))
    plot(predictions, target)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report_markdown(summary, scores, phys_daily), encoding="utf-8")
    print(f"已輸出 {REPORT.relative_to(ROOT)}、{FIGURE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
