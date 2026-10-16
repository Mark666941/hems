"""用電資料來源介面（模擬 / 感測器）與 ASHRAE Guideline 14 模型校正。"""
from datetime import date

import pandas as pd
import pytest

from core.data_source import METER_COLUMNS, CsvMeterSource, CwaCsvWeatherSource
from core.metrics import calibration_metrics, compare_with_meter
from core.simulator import SimulatedMeterSource, Simulator

LIMITS = {"nmbe_pct": 10, "cvrmse_pct": 30}
DAY = date(2026, 7, 20)


@pytest.fixture(scope="module")
def simulated():
    return SimulatedMeterSource(Simulator())


def test_weather_available_dates_cover_jan_to_sep():
    days = CwaCsvWeatherSource().available_dates()
    assert days[0] == date(2026, 1, 1)
    assert days[-1] == date(2026, 9, 30)
    assert len(days) == 273


def test_simulated_meter_matches_simulation(simulated):
    df = simulated.get_hourly_consumption(DAY)
    assert list(df.columns) == METER_COLUMNS
    assert df["kwh"].sum() == pytest.approx(Simulator().run(DAY).total_kwh)


def test_csv_meter_integrates_power_samples(tmp_path):
    # 15 分鐘一筆、固定 1000 W → 每小時 1 kWh
    stamps = pd.date_range("2026-07-20 00:00", "2026-07-20 23:45", freq="15min")
    path = tmp_path / "meter.csv"
    pd.DataFrame({"timestamp": stamps, "space_id": "kitchen", "appliance_id": "refrigerator",
                  "power_w": 1000.0}).to_csv(path, index=False)
    meter = CsvMeterSource(path)
    hourly = meter.get_hourly_consumption(DAY)
    assert len(hourly) == 24
    assert hourly["kwh"].tolist() == pytest.approx([1.0] * 24)
    assert meter.available_dates() == [DAY]


def test_csv_meter_accepts_kwh_and_validates_columns(tmp_path):
    path = tmp_path / "kwh.csv"
    pd.DataFrame({"timestamp": ["2026-07-20T10:00", "2026-07-20T10:30"], "space_id": "living_room",
                  "appliance_id": "main", "kwh": [0.4, 0.6]}).to_csv(path, index=False)
    assert CsvMeterSource(path).get_hourly_consumption(DAY)["kwh"].tolist() == [pytest.approx(1.0)]

    bad = tmp_path / "bad.csv"
    pd.DataFrame({"timestamp": ["2026-07-20T10:00"], "space_id": "x"}).to_csv(bad, index=False)
    with pytest.raises(ValueError, match="欄位不足"):
        CsvMeterSource(bad)


def test_calibration_metrics_formula():
    measured, simulated = [10, 12, 8, 10], [9, 12, 9, 10]
    m = calibration_metrics(measured, simulated, LIMITS)
    # NMBE = Σ(m−s) / ((n−1)·平均) = 0 / … = 0；CV(RMSE) = √(2/3) / 10
    assert m["NMBE(%)"] == pytest.approx(0)
    assert m["CV(RMSE)(%)"] == pytest.approx(100 * (2 / 3) ** 0.5 / 10)
    assert m["通過"]
    biased = calibration_metrics([10] * 10, [8] * 10, LIMITS)   # 模型低估 20%
    assert biased["NMBE(%)"] > 10 and not biased["通過"]


def test_perfect_model_passes_calibration(simulated):
    # 用模擬資料當「實測」，誤差應為 0
    detail, summary = compare_with_meter(_FakeMeter(simulated), simulated, [DAY], LIMITS)
    assert summary["NMBE(%)"].abs().max() < 1e-9
    assert summary["CV(RMSE)(%)"].max() < 1e-9
    assert summary["通過"].all()


class _FakeMeter:
    """把模擬資料轉成總電表 + 冷氣插座兩個通道，當作「完美實測」。"""

    def __init__(self, simulated):
        self.simulated = simulated

    def get_hourly_consumption(self, day):
        df = self.simulated.get_hourly_consumption(day)
        main = df.groupby("hour", as_index=False)["kwh"].sum().assign(space_id="main", appliance_id="main")
        ac = df[(df["space_id"] == "living_room") & (df["appliance_id"] == "air_conditioner")]
        return pd.concat([main[METER_COLUMNS], ac[METER_COLUMNS]], ignore_index=True)
