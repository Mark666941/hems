"""B1b：物理模擬 + 梯度提升樹殘差修正的混合用電預測（時間序列交叉驗證）。"""
import importlib.util
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ml_forecast", ROOT / "scripts" / "ml_forecast.py")
ml = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ml)


@pytest.fixture(scope="module")
def phys_daily():
    return ml.physics_daily_by_month()


@pytest.fixture(scope="module")
def features(phys_daily):
    return ml.build_features(ml.load_target(), phys_daily)


@pytest.fixture(scope="module")
def cv(features):
    return ml.cross_validate(features)


def test_physics_feature_covers_all_months(phys_daily):
    assert sorted(phys_daily) == list(range(1, 13))
    assert all(5 < v < 40 for v in phys_daily.values())          # 4 人家庭每天 5–40 度
    assert phys_daily[10] == phys_daily[9]                        # 10 月沒有天氣資料，用 9 月代替


def test_features(features, phys_daily):
    row = features[features["month"] == "2026-03"].iloc[0]
    assert row["phys"] == pytest.approx(phys_daily[3] * 31)
    assert row["phys_prev"] == pytest.approx(phys_daily[2] * 28)   # 上月（2 月 28 天）
    assert features["t"].is_monotonic_increasing
    assert not features[ml.ML_FEATURES + ml.PHYS_FEATURES + ["kwh"]].isna().any().any()


def test_cross_validation_only_predicts_future(features):
    from sklearn.model_selection import TimeSeriesSplit
    for tr, te in TimeSeriesSplit(n_splits=5, test_size=12).split(features):
        assert features.iloc[tr]["month"].max() < features.iloc[te]["month"].min()   # 不偷看未來


def test_models_compared(cv):
    scores, predictions = cv
    assert len(scores) == 5 * len(ml.MODELS)
    assert set(predictions["模型"]) == set(ml.MODELS) and len(predictions) == 5 * 12 * len(ml.MODELS)
    summary = ml.summarize(scores).set_index("模型")
    assert np.isfinite(summary.to_numpy()).all()
    assert (summary["MAPE(%)"] < 50).all()
    # 混合模型用 ML 修正物理模型沒解釋到的部分，應比純物理準
    assert summary.loc["混合（物理＋ML 殘差）", "MAPE(%)"] < summary.loc["純物理", "MAPE(%)"]


def test_report_and_plot(cv, phys_daily, tmp_path):
    scores, predictions = cv
    text = ml.report_markdown(ml.summarize(scores), scores, phys_daily)
    for word in ("MAPE", "純物理", "純 ML", "混合", "時間序列交叉驗證"):
        assert word in text
    path = ml.plot(predictions, ml.load_target(), tmp_path / "ml.png")
    assert path.stat().st_size > 10_000
