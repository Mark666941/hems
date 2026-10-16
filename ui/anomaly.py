"""用電異常偵測（B2）：以「實測 − 模擬」的預測誤差做統計門檻。

物理模擬就是每個小時的「預測值」；實測（智慧插座、電表）和預測差太多的時段就是異常。
為了不被少數極端值與模型的系統性偏差（例如冷氣整體偏高 15%）影響，用穩健統計量：
- 中心：每個通道誤差的中位數（系統性偏差會被扣掉，只看「突然不一樣」的時段）
- 尺度：σ ≈ 1.4826 × MAD（中位數絕對離差；常態分布時等於標準差），並設最小值避免誤差太小時過度敏感
偵測三種異常：
1. 單一小時：|誤差 − 中位數| > k σ（預設 k = 3，常態下約 0.3% 的機率是誤報）
2. 持續偏離：連續 ≥ 3 小時同方向超過 2σ（例如家電忘了關、待機耗電突然變大）
3. 整天偏離：當天誤差合計的穩健 z 分數 > 2.5（資料至少 5 天才判斷）
"""
from __future__ import annotations

import pandas as pd

MAD_TO_SIGMA = 1.4826
MIN_SCALE_KWH = 0.02           # σ 最小 0.02 kWh（20 Wh），避免幾乎不用電的通道一點差異就報警
MIN_SCALE_RATIO = 0.05         # 或該通道平均每小時實測用電的 5%
MIN_DAY_RATIO = 0.02           # 整天合計：σ 最小為平均每天實測用電的 2%


def _robust(values: pd.Series, floor: float) -> tuple[float, float]:
    center = float(values.median())
    scale = MAD_TO_SIGMA * float((values - center).abs().median())
    return center, max(scale, floor)


def score(detail: pd.DataFrame) -> pd.DataFrame:
    """逐時對照表（date、hour、channel、measured_kwh、simulated_kwh）加上誤差、中心、尺度與 z 分數。"""
    df = detail.copy()
    df["residual"] = df["measured_kwh"] - df["simulated_kwh"]
    parts = []
    for _, g in df.groupby("channel", sort=False):
        floor = max(MIN_SCALE_KWH, MIN_SCALE_RATIO * float(g["measured_kwh"].mean()))
        center, scale = _robust(g["residual"], floor)
        parts.append(g.assign(center=center, scale=scale, z=(g["residual"] - center) / scale))
    return pd.concat(parts).sort_values(["channel", "date", "hour"]).reset_index(drop=True)


def _runs(flags: list[int]) -> list[tuple[int, int, int]]:
    """連續相同非零值的區段：[(開始索引, 結束索引, 方向)]。"""
    runs, i = [], 0
    while i < len(flags):
        if flags[i] == 0:
            i += 1
            continue
        j = i
        while j + 1 < len(flags) and flags[j + 1] == flags[i]:
            j += 1
        runs.append((i, j, flags[i]))
        i = j + 1
    return runs


def detect(detail: pd.DataFrame, k: float = 3.0, run_k: float = 2.0, min_run: int = 3,
           day_k: float = 2.5, names: dict[str, str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """回傳（加上 z 分數的逐時表, 異常清單）。names：通道 → 顯示名稱。"""
    names = names or {}
    scored = score(detail)
    rows = []

    def add(kind, channel, day, hours, meas, sim, z, note):
        direction = "偏高" if z > 0 else "偏低"
        rows.append({"類型": kind, "通道": names.get(channel, channel), "channel": channel, "日期": day,
                     "時段": hours, "實測(kWh)": meas, "模擬(kWh)": sim, "差異(kWh)": meas - sim,
                     "偏離(σ)": z, "方向": direction, "說明": note.format(dir=direction)})

    for channel, g in scored.groupby("channel", sort=False):
        # 1. 單一小時
        for r in g[g["z"].abs() > k].itertuples():
            add("單一小時", channel, r.date, f"{r.hour:02d}:00", r.measured_kwh, r.simulated_kwh, r.z,
                f"這一小時用電比預測{{dir}}很多（超過 {k:g}σ），可能是臨時使用家電或設備異常")
        # 2. 持續偏離（每天分開看）
        for day, d in g.groupby("date", sort=False):
            d = d.sort_values("hour").reset_index(drop=True)
            flags = [1 if z > run_k else -1 if z < -run_k else 0 for z in d["z"]]
            for s, e, _sign in _runs(flags):
                if e - s + 1 < min_run:
                    continue
                part = d.iloc[s:e + 1]
                add("持續偏離", channel, day, f"{int(part['hour'].iloc[0]):02d}:00–{int(part['hour'].iloc[-1]) + 1:02d}:00",
                    float(part["measured_kwh"].sum()), float(part["simulated_kwh"].sum()), float(part["z"].mean()),
                    f"連續 {e - s + 1} 小時{{dir}}超過 {run_k:g}σ，可能是家電忘了關、待機耗電變大或作息改變")
        # 3. 整天偏離
        daily = g.groupby("date")[["measured_kwh", "simulated_kwh", "residual"]].sum()
        if len(daily) >= 5:
            # 每小時誤差大致獨立，整天合計的尺度約為每小時的 √24 倍；另設全天用電 2% 為最小值
            floor = max(MIN_SCALE_KWH * 24 ** 0.5, MIN_DAY_RATIO * float(daily["measured_kwh"].mean()))
            center, scale = _robust(daily["residual"], floor)
            for day, r in daily.iterrows():
                z = (r["residual"] - center) / scale
                if abs(z) > day_k:
                    add("整天偏離", channel, day, "全天", float(r["measured_kwh"]), float(r["simulated_kwh"]), z,
                        f"這一天的總用電比平常的誤差{{dir}}（超過 {day_k:g}σ），可以檢查當天是否有特別的活動")

    columns = ["類型", "通道", "channel", "日期", "時段", "實測(kWh)", "模擬(kWh)", "差異(kWh)", "偏離(σ)", "方向", "說明"]
    anomalies = pd.DataFrame(rows, columns=columns)
    if not anomalies.empty:
        anomalies = anomalies.reindex(anomalies["偏離(σ)"].abs().sort_values(ascending=False).index)
    return scored, anomalies.reset_index(drop=True)


def channel_names(house) -> dict[str, str]:
    """通道代碼 → 顯示名稱（main/main＝總電表）。"""
    names = {"main/main": "總電表"}
    for s, a in house.all_appliances():
        names[f"{s.id}/{a.id}"] = f"{s.name}・{a.name}"
    return names
