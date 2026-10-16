"""產生「示範用合成電表資料」，用來展示模型校正流程（不是實測資料）。

做法：以基準情境推演一週，再加入真實世界常見的差異：
- 冷氣實際耗電比模型高 15%（例如濾網髒、實際冷房負載較大）
- 電熱水器開始時間前後飄移 ±30 分鐘
- 模型沒有計入的待機耗電 30 W（電視、充電器待機等）
- 各通道每小時 ±10% 隨機波動
輸出 15 分鐘間隔的功率取樣（power_w），模擬智慧電表的匯出格式。

用法：python scripts/make_demo_meter_data.py
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.simulator import Simulator  # noqa: E402

DAYS = [date(2026, 7, 13) + timedelta(days=i) for i in range(7)]
CHANNELS = [("main", "main"), ("master_bedroom", "air_conditioner"),
            ("living_room", "air_conditioner"), ("utility", "water_heater")]
AC_BIAS, STANDBY_W, NOISE, SEED = 1.15, 30, 0.10, 2026


def main() -> None:
    rng = np.random.default_rng(SEED)
    sim = Simulator()
    rows = []
    for day in DAYS:
        result = sim.run(day)
        records = result.records.copy()
        # 冷氣偏高、熱水器時間飄移
        records.loc[records["appliance_id"] == "air_conditioner", "kwh"] *= AC_BIAS
        shift = int(rng.choice([-1, 0, 1]))
        heater = records["appliance_id"] == "water_heater"
        records.loc[heater, "hour"] = (records.loc[heater, "hour"] + shift) % 24
        records["kwh"] *= rng.normal(1, NOISE, len(records)).clip(0.5, 1.5)

        for space_id, appliance_id in CHANNELS:
            if appliance_id == "main":
                hourly = records.groupby("hour")["kwh"].sum() + STANDBY_W / 1000
            else:
                part = records[(records["space_id"] == space_id) & (records["appliance_id"] == appliance_id)]
                hourly = part.groupby("hour")["kwh"].sum()
            for h in range(24):
                kwh = float(hourly.get(h, 0.0))
                # 一小時拆成 4 筆 15 分鐘平均功率，加一點小波動但保持該小時總能量不變
                weights = rng.uniform(0.8, 1.2, 4) if kwh > 0 else np.ones(4)
                weights /= weights.sum()
                for q in range(4):
                    ts = datetime(day.year, day.month, day.day, h, 15 * q)
                    rows.append({"timestamp": ts.isoformat(), "space_id": space_id, "appliance_id": appliance_id,
                                 "power_w": round(kwh * weights[q] * 4 * 1000, 1)})

    out_dir = ROOT / "data" / "meter"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "demo_synthetic_2026-07.csv"
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")
    print(f"已產生 {len(rows)} 筆示範用合成資料 → {path}")


if __name__ == "__main__":
    main()
