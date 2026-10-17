"""新功能：匯入電表資料與自動校正。"""
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from core.calibration import Calibration, calibrate, demo_ami_csv, read_meter_csv
from core.simulator import Simulator, apply_calibration, load_baseline_plan

DAYS = [date(2026, 7, d) for d in range(13, 20)]


@pytest.fixture(scope="module")
def sim():
    return Simulator()


@pytest.fixture(scope="module")
def demo_csv(sim):
    return demo_ami_csv(sim, DAYS, "tou_2", load_baseline_plan())


def test_read_15min_interval_ending(demo_csv):
    hourly = read_meter_csv(demo_csv.encode("utf-8"))
    assert hourly.attrs["interval_min"] == 15
    assert sorted(set(hourly["date"])) == DAYS                       # 24:00 歸回前一天，7 天都完整
    assert len(hourly) == 7 * 24 and set(hourly["hour"]) == set(range(24))


def test_read_hourly_power_big5_two_columns():
    rows = ["日期,時間,平均功率(W)"] + [f"2026/07/20,{h:02d}:00,{500 + 10 * h}" for h in range(24)]
    hourly = read_meter_csv("\n".join(rows).encode("cp950"))
    assert hourly.attrs["interval_min"] == 60
    assert hourly.loc[hourly["hour"] == 3, "kwh"].item() == pytest.approx(0.53)   # 530 W × 1 h


def test_bad_files_explain_problem():
    with pytest.raises(ValueError, match="時間"):
        read_meter_csv(b"a,b\nx,y\n")
    with pytest.raises(ValueError):
        read_meter_csv("時間,度\n2026/07/20 01:00,0.5\n".encode())    # 資料不足


def test_calibration_recovers_known_differences(sim, demo_csv):
    measured = read_meter_csv(demo_csv.encode("utf-8"))
    limits = json.loads(Path("config/metrics.json").read_text(encoding="utf-8"))["calibration"]["hourly"]
    out = calibrate(sim, measured, set(DAYS), "tou_2", load_baseline_plan(), limits)
    cal = out["calibration"]
    assert cal.ac_factor == pytest.approx(1.25, abs=0.08)
    assert cal.other_factor == pytest.approx(0.9, abs=0.08)
    assert cal.base_kw == pytest.approx(0.06, abs=0.04)
    assert abs(out["after"]["NMBE(%)"]) < abs(out["before"]["NMBE(%)"])
    assert out["after"]["CV(RMSE)(%)"] < out["before"]["CV(RMSE)(%)"]
    assert out["after"]["通過"]                                      # 校正後符合 Guideline 14 逐時標準


def test_apply_calibration_scales_records(sim):
    r = sim.run(DAYS[0], "tou_2", load_baseline_plan(), calibrate=False)
    cal = Calibration(1.2, 0.8, 0.05).as_settings()
    out = apply_calibration(r.records, cal)
    ac = r.records["appliance_id"] == "air_conditioner"
    expected = 1.2 * r.records.loc[ac, "kwh"].sum() + 0.8 * r.records.loc[~ac, "kwh"].sum() + 0.05 * 24
    assert out["kwh"].sum() == pytest.approx(expected)
    assert (out["appliance_id"] == "unmodeled_base").sum() == 24


def test_settings_calibration_changes_simulation():
    from ui.settings import build_analyzer, default_settings
    s = default_settings()
    plain = build_analyzer(s).sim.run(DAYS[0]).total_kwh
    s["calibration"] = {"ac_factor": 1.0, "other_factor": 1.0, "base_w": 100.0}
    assert build_analyzer(s).sim.run(DAYS[0]).total_kwh == pytest.approx(plain + 2.4)   # 100 W × 24 h
    assert isinstance(pd.Timestamp(DAYS[0]), pd.Timestamp)


def test_calibration_tab_with_demo_data():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=300)
    at.session_state["_test_page"] = "analysis"
    at.run()
    toggle = next(t for t in at.toggle if t.label == "使用示範資料")
    toggle.set_value(True).run()
    assert not at.exception and not at.error
    labels = {m.label: m.value for m in at.metric}
    assert float(labels["冷氣用電倍率 α"]) == pytest.approx(1.25, abs=0.1)
    assert any(b.label == "套用校正" for b in at.button)
