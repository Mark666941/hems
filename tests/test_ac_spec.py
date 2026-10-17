"""A5：冷氣規格（能力、能效等級）在設定與模擬中的效果。"""
from datetime import date

import pytest

from ui.settings import (
    ac_spec_rows,
    apply_ac_specs,
    build_analyzer,
    build_house,
    default_settings,
    suggested_capacity_kw,
)

OPTIONS = [2.2, 2.8, 3.6, 4.1, 5.0, 5.6, 6.3, 7.1, 8.0]
DAY = date(2026, 7, 20)


def test_suggested_capacity():
    assert suggested_capacity_kw(4, OPTIONS) == 2.2        # 4 坪 × 0.55 = 2.2 kW，剛好是市售規格
    assert suggested_capacity_kw(8, OPTIONS) == 5.0        # 8 坪 × 0.55 = 4.4 → 5.0
    assert suggested_capacity_kw(30, OPTIONS) == 8.0       # 超過最大規格時取最大


def test_apply_specs_keeps_other_edits():
    s = default_settings()
    s["appliance_edits"]["bedroom_1/air_conditioner"] = {"power_w": 900, "enabled": True}
    rows = ac_spec_rows(s, OPTIONS)
    assert {r["空間"] for r in rows} == {"主臥", "次臥 1", "次臥 2", "客廳"}
    rows = [{**r, "能力(kW)": 3.6, "能效等級": 1} if r["key"] == "bedroom_1/air_conditioner" else r for r in rows]
    new = apply_ac_specs(s, rows)
    edit = new["appliance_edits"]["bedroom_1/air_conditioner"]
    assert edit == {"power_w": 900, "enabled": True, "capacity_kw": 3.6, "grade": 1}
    unit = build_house(new).get_space("bedroom_1").get_appliance("air_conditioner")
    assert unit.capacity_kw == 3.6 and unit.grade == 1
    assert new["appliance_edits"]["master_bedroom/air_conditioner"]["capacity_kw"] is None   # 空白＝沿用功率


def test_better_grade_saves_energy():
    def ac_kwh(grade):
        s = default_settings()
        rows = [{**r, "能力(kW)": r["建議能力(kW)"], "能效等級": grade} for r in ac_spec_rows(s, OPTIONS)]
        result = build_analyzer(apply_ac_specs(s, rows)).sim.run(DAY, "tou_2")
        return result.records.query("appliance_id == 'air_conditioner'")["kwh"].sum()
    g1, g5 = ac_kwh(1), ac_kwh(5)
    assert g1 == pytest.approx(g5 * 3.0 / 4.3, rel=0.03)   # 耗電約與額定 COP 成反比（部分負載率略有不同）
