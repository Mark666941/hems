"""B2：用電異常偵測（實測 − 模擬的預測誤差，穩健統計門檻）。"""
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from core.data_source import CsvMeterSource
from core.metrics import compare_with_meter
from core.simulator import SimulatedMeterSource
from ui import anomaly
from ui.settings import build_analyzer, default_settings

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "data" / "meter" / "demo_synthetic_2026-07.csv"


def _detail(days: int = 7, bias: float = 0.0, seed: int = 0) -> pd.DataFrame:
    """合成逐時對照表：模擬 = 0.5 kWh；實測 = 模擬 × (1 + bias) + 小幅隨機誤差。"""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(days):
        for h in range(24):
            sim = 0.5
            rows.append({"date": date(2026, 7, 1) + timedelta(d), "hour": h, "channel": "main/main",
                         "measured_kwh": sim * (1 + bias) + rng.normal(0, 0.02), "simulated_kwh": sim})
    return pd.DataFrame(rows)


def test_normal_noise_has_no_anomalies():
    _, found = anomaly.detect(_detail())
    assert (found["類型"] == "單一小時").sum() <= 1          # 3σ 門檻：168 筆隨機誤差幾乎不會誤報


def test_systematic_bias_is_not_an_anomaly():
    """模型整體偏低 15%（例如冷氣耗電低估）不算異常：中位數會吸收系統性偏差。"""
    scored, found = anomaly.detect(_detail(bias=0.15))
    assert scored["center"].iloc[0] == pytest.approx(0.075, abs=0.01)
    assert (found["類型"] == "單一小時").sum() <= 1


def test_spike_and_sustained_run_detected():
    df = _detail()
    spike = (df["date"] == date(2026, 7, 3)) & (df["hour"] == 14)
    df.loc[spike, "measured_kwh"] += 1.5                       # 某一小時多用 1.5 度
    run = (df["date"] == date(2026, 7, 5)) & df["hour"].between(1, 4)
    df.loc[run, "measured_kwh"] += 0.15                        # 半夜連續 4 小時多 150 W（家電忘了關）
    _, found = anomaly.detect(df, names={"main/main": "總電表"})
    hourly = found[found["類型"] == "單一小時"]
    assert ((hourly["日期"] == date(2026, 7, 3)) & (hourly["時段"] == "14:00")).any()
    assert hourly.iloc[0]["方向"] == "偏高" and hourly.iloc[0]["通道"] == "總電表"
    sustained = found[found["類型"] == "持續偏離"]
    assert len(sustained) == 1 and sustained.iloc[0]["時段"] == "01:00–05:00"
    assert found.iloc[0]["偏離(σ)"] == found["偏離(σ)"].abs().max()   # 依偏離程度排序


def test_whole_day_deviation():
    df = _detail(days=10)
    df.loc[df["date"] == date(2026, 7, 8), "measured_kwh"] -= 0.06   # 整天少用（例如出遊）
    _, found = anomaly.detect(df)
    day = found[found["類型"] == "整天偏離"]
    assert list(day["日期"]) == [date(2026, 7, 8)] and day.iloc[0]["方向"] == "偏低"


def test_runs_helper():
    assert anomaly._runs([0, 1, 1, 1, 0, -1, -1, 1]) == [(1, 3, 1), (5, 6, -1), (7, 7, 1)]
    assert anomaly._runs([]) == []


def test_demo_meter_data():
    """示範合成資料（冷氣 +15%、熱水器時間飄移、待機 30 W）：可以跑完偵測，系統性偏差為正。"""
    a = build_analyzer(default_settings())
    measured = CsvMeterSource(DEMO)
    detail, _ = compare_with_meter(measured, SimulatedMeterSource(a.sim, control=a.opt.baseline_plan),
                                   measured.available_dates(), a.cfg["calibration"]["hourly"])
    scored, found = anomaly.detect(detail, names=anomaly.channel_names(a.sim.house))
    assert set(scored["channel"]) >= {"main/main", "utility/water_heater"}
    main = scored[scored["channel"] == "main/main"]
    assert main["center"].iloc[0] > 0                           # 實測整體比模擬高（待機、冷氣偏高）
    assert set(found["類型"]) <= {"單一小時", "持續偏離", "整天偏離"}


def test_anomaly_tab_renders():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180)
    at.session_state["_test_page"] = "analysis"
    at.run()
    assert not at.exception and not at.error
    assert any(m.label == "單一小時異常" for m in at.metric)
    at.slider(key="anomaly_k").set_value(2.0).run()
    at.selectbox(key="anomaly_channel").set_value("utility/water_heater").run()
    assert not at.exception and not at.error
