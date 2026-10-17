"""匯入電表資料與自動校正（新功能）：讓系統從「典型家庭」變成「你的家」。

1. 讀檔（read_meter_csv）：台電智慧電表（AMI）可在台電網站或 App 查詢每 15 分鐘用電，匯出格式不一，
   因此自動辨識欄位：時間（合併一欄或日期、時間兩欄）、用電（度／kWh，或功率 W／kW 換算），
   間隔 15 分鐘到 1 小時皆可；UTF-8 或 Big5 編碼。時間若標在區間結束（00:15、…、24:00），自動往前對齊。
2. 比對：彙整成逐時全屋用電，與模擬（一般習慣）逐時比較，算 ASHRAE Guideline 14 指標（NMBE、CV(RMSE)）。
3. 校正（fit_calibration）：最小平方法把實測拆成
       實測(h) ≈ α × 模擬冷氣(h) + β × 模擬其他家電(h) + γ（未建模的固定待機，kW）
   α、β 表示模型高估或低估的倍率，γ 是模型沒有的常開電器（例如魚缸、伺服器）。
   係數限制在合理範圍內，避免過度擬合；套用後推演的用電與電費依此修正。
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd
from scipy.optimize import lsq_linear

from core.metrics import calibration_metrics

TIME_WORDS = ("時間", "日期", "時段", "datetime", "timestamp", "time", "date")
ENERGY_WORDS = ("度", "kwh", "用電", "電量", "energy")
POWER_WORDS = ("功率", "power", "kw", "(w)", "（w）", "_w", " w")
BOUNDS = {"ac_factor": (0.3, 3.0), "other_factor": (0.3, 3.0), "base_kw": (0.0, 1.0)}


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp950", "big5"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("無法辨識檔案編碼（請另存為 UTF-8 或 Big5 的 CSV）")


def _find(columns: list[str], words: tuple[str, ...], exclude: set[str] = frozenset()) -> list[str]:
    return [c for c in columns if c not in exclude and any(w in c.lower() for w in words)]


def read_meter_csv(data: bytes) -> pd.DataFrame:
    """讀取電表 CSV，回傳逐時全屋用電：date（datetime.date）、hour（0–23）、kwh；另附 attrs["interval_min"]。"""
    df = pd.read_csv(io.StringIO(_decode(data)), sep=None, engine="python")
    df.columns = [str(c).strip() for c in df.columns]
    cols = list(df.columns)
    time_cols = _find(cols, TIME_WORDS)
    if not time_cols:                             # 沒有可辨識的欄名：找第一個能轉成時間的欄位
        time_cols = [c for c in cols if pd.to_datetime(df[c], errors="coerce").notna().mean() > 0.9][:1]
    if not time_cols:
        raise ValueError("找不到時間欄位（欄名需含「時間」「日期」或 time、date）")
    date_col = next((c for c in time_cols if "日期" in c or c.lower() == "date"), None)
    clock_col = next((c for c in time_cols if c != date_col and ("時間" in c or "time" in c.lower())), None)
    if date_col and clock_col:                    # 日期、時間分兩欄
        raw = df[date_col].astype(str).str.strip() + " " + df[clock_col].astype(str).str.strip()
    else:
        raw = df[time_cols[0]].astype(str).str.strip()
    # 24:00 表示當天結束（隔天 00:00）
    is_24 = raw.str.contains(r"\b24:00")
    ts = pd.to_datetime(raw.str.replace(r"\b24:00(:00)?", "00:00", regex=True), errors="coerce")
    ts = ts.where(~is_24, ts + pd.Timedelta(days=1))
    value_cols = [c for c in cols if c not in time_cols and pd.to_numeric(df[c], errors="coerce").notna().mean() > 0.9]
    energy = _find(value_cols, ENERGY_WORDS)
    power = [c for c in _find(value_cols, POWER_WORDS) if c not in energy or "kwh" not in c.lower()]
    if energy:
        col, kind = energy[0], "energy"
    elif power:
        col, kind = power[0], "power"
    elif value_cols:
        col, kind = value_cols[0], "energy"
    else:
        raise ValueError("找不到用電欄位（欄名需含「度」「kWh」「用電」或功率 W、kW）")
    out = pd.DataFrame({"ts": ts, "v": pd.to_numeric(df[col], errors="coerce")}).dropna().sort_values("ts")
    if len(out) < 4:
        raise ValueError("有效資料不足（至少需要數小時的資料）")
    step = out["ts"].diff().dropna().mode().iloc[0]
    minutes = max(1, int(step.total_seconds() // 60))
    if kind == "power":                           # 功率 → 該區間用電
        scale = 1 / 1000 if "kw" not in col.lower() else 1.0
        out["v"] = out["v"] * scale * minutes / 60
    # 時間標在區間結束（每天沒有 00:00、或出現 24:00）：往前一個間隔，對齊「區間開始」
    times = out["ts"].dt.strftime("%H:%M")
    if is_24.any() or ("00:00" not in set(times) and minutes < 60):
        out["ts"] = out["ts"] - step
    out["date"], out["hour"] = out["ts"].dt.date, out["ts"].dt.hour
    hourly = out.groupby(["date", "hour"], as_index=False)["v"].sum().rename(columns={"v": "kwh"})
    counts = out.groupby(["date"])["v"].size()
    complete = counts[counts >= (24 * 60 // minutes) * 0.95].index   # 只保留資料齊全的日子
    hourly = hourly[hourly["date"].isin(set(complete))].reset_index(drop=True)
    if hourly.empty:
        raise ValueError("沒有資料完整的一天（每天需要 24 小時的資料）")
    hourly.attrs["interval_min"] = minutes
    hourly.attrs["column"] = col
    return hourly


@dataclass
class Calibration:
    ac_factor: float = 1.0       # 冷氣用電倍率 α
    other_factor: float = 1.0    # 其他家電倍率 β
    base_kw: float = 0.0         # 未建模固定待機 γ（kW）

    def as_settings(self) -> dict:
        return {"ac_factor": round(self.ac_factor, 3), "other_factor": round(self.other_factor, 3),
                "base_w": round(self.base_kw * 1000, 1)}


def simulated_components(sim, days: list[date], plan_id: str, control) -> pd.DataFrame:
    """模擬（一般習慣）逐時的冷氣與其他家電用電：date、hour、ac_kwh、other_kwh（不含任何校正）。"""
    rows = []
    for day in days:
        r = sim.run(day, plan_id, control, calibrate=False)
        rec = r.records.assign(is_ac=r.records["appliance_id"] == "air_conditioner")
        g = rec.groupby(["hour", "is_ac"])["kwh"].sum().unstack(fill_value=0.0).reindex(range(24), fill_value=0.0)
        for h in range(24):
            rows.append({"date": day, "hour": h, "ac_kwh": float(g.get(True, pd.Series(0.0, index=range(24)))[h]),
                         "other_kwh": float(g.get(False, pd.Series(0.0, index=range(24)))[h])})
    return pd.DataFrame(rows)


def fit_calibration(merged: pd.DataFrame) -> Calibration:
    """有界最小平方法：實測 ≈ α × 冷氣 + β × 其他 + γ。merged 需有 kwh、ac_kwh、other_kwh。"""
    a = np.column_stack([merged["ac_kwh"], merged["other_kwh"], np.ones(len(merged))])
    lo = [BOUNDS["ac_factor"][0], BOUNDS["other_factor"][0], BOUNDS["base_kw"][0]]
    hi = [BOUNDS["ac_factor"][1], BOUNDS["other_factor"][1], BOUNDS["base_kw"][1]]
    if merged["ac_kwh"].sum() < 1e-6:            # 冷氣沒開的季節：α 無法估計，維持 1
        lo[0], hi[0] = 1.0 - 1e-9, 1.0 + 1e-9
    res = lsq_linear(a, merged["kwh"].to_numpy(), bounds=(lo, hi))
    return Calibration(*map(float, res.x))


def evaluate(merged: pd.DataFrame, cal: Calibration, limits: dict) -> dict:
    predicted = cal.ac_factor * merged["ac_kwh"] + cal.other_factor * merged["other_kwh"] + cal.base_kw
    return calibration_metrics(merged["kwh"], predicted, limits, n_params=3 if cal != Calibration() else 1)


def calibrate(sim, measured: pd.DataFrame, available: set[date], plan_id: str, control, limits: dict) -> dict:
    """完整流程：取實測與天氣都有的日子 → 模擬 → 擬合 → 校正前後指標。"""
    days = sorted(d for d in set(measured["date"]) if d in available)
    if not days:
        raise ValueError("電表資料的日期沒有對應的天氣資料（可到「地區與天氣」改用 Open-Meteo，涵蓋到今天）")
    sim_df = simulated_components(sim, days, plan_id, control)
    merged = measured.merge(sim_df, on=["date", "hour"])
    cal = fit_calibration(merged)
    merged["before_kwh"] = merged["ac_kwh"] + merged["other_kwh"]
    merged["after_kwh"] = cal.ac_factor * merged["ac_kwh"] + cal.other_factor * merged["other_kwh"] + cal.base_kw
    return {"days": days, "merged": merged, "calibration": cal,
            "before": evaluate(merged, Calibration(), limits), "after": evaluate(merged, cal, limits)}


def demo_ami_csv(sim, days: list[date], plan_id: str, control, seed: int = 7) -> str:
    """示範用合成 AMI 資料（不是實測）：模擬值加上刻意差異——冷氣 ×1.25、其他家電 ×0.9、
    多 60 W 固定待機與 ±8% 波動，每 15 分鐘一筆、時間標在區間結束（同台電慣例），用來展示校正能找回這些差異。"""
    rng = np.random.default_rng(seed)
    comp = simulated_components(sim, days, plan_id, control)
    lines = ["時間,用電度數(kWh)"]
    for row in comp.itertuples():
        hour_kwh = 1.25 * row.ac_kwh + 0.9 * row.other_kwh + 0.06
        for q in range(4):
            end = pd.Timestamp(row.date) + timedelta(hours=row.hour, minutes=15 * (q + 1))
            stamp = end.strftime("%Y/%m/%d %H:%M") if end.date() == row.date else f"{row.date:%Y/%m/%d} 24:00"
            lines.append(f"{stamp},{hour_kwh / 4 * (1 + rng.normal(0, 0.08)):.4f}")
    return "\n".join(lines) + "\n"
