"""C5：整月分析報告匯出 PDF（matplotlib 內建 PDF 輸出）。"""
import re
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from core.metrics import Recommendation
from ui import insights
from ui.pdf_report import month_report_pdf
from ui.settings import build_analyzer, default_settings

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def daily():
    a = build_analyzer(default_settings())
    days = [date(2026, 7, d) for d in range(1, 8)]
    return insights.daily_table({
        insights.BASELINE: [a.sim.run(d, "tou_2", a.opt.baseline_plan) for d in days],
        "省電模式": [a.opt.optimize(d, "saving", "tou_2").result for d in days]})


def _mtable():
    return pd.DataFrame({"模式": [insights.BASELINE, "省電模式"], "月用電(kWh)": [560.0, 480.0],
                         "二段式時間電價(元)": [2100.0, 1700.0], "平均舒適時間比例(%)": [97.0, 93.0],
                         "碳排放(kg)": [266.0, 228.0]})


def _pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf))


def test_pdf_has_all_sections(daily):
    recs = [Recommendation("改用省電模式", "每天各自最佳化。" * 5, 80, 400, note="已包含冷氣調高 1°C")]
    found = insights.month_insights(daily, "省電模式", 3.0)
    pdf = month_report_pdf(2026, 7, "標配房型", "二段式時間電價", _mtable(), recs, daily, found)
    assert pdf.startswith(b"%PDF") and len(pdf) > 20_000
    assert _pages(pdf) == 3                                   # 摘要、圖表、建議
    assert b"FontFile2" in pdf                                # 嵌入 TrueType 字型（中文可顯示、可搜尋）


def test_long_recommendations_flow_to_next_page(daily):
    recs = [Recommendation(f"建議 {i}", "很長的說明文字，" * 40, 10, 50) for i in range(12)]
    pdf = month_report_pdf(2026, 7, "標配房型", "二段式時間電價", _mtable(), recs, daily, [])
    assert _pages(pdf) >= 4                                   # 建議太多時自動換頁


def test_pdf_download_in_month_analysis():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300)
    at.session_state["_test_page"] = "analysis"
    at.run()
    next(b for b in at.button if b.label == "執行整月分析").click().run()
    assert not at.exception and not at.error
    labels = [e.proto.label for e in at.get("download_button")]
    assert "下載分析報告（PDF）" in labels and "下載分析報告（Markdown）" in labels
