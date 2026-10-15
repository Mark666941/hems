"""階段 3：作息模型。"""
import json
from datetime import date

import pytest

from core.models import load_house
from core.schedule import CONFIG_DIR, Schedule, day_type_for
from core.tariff import Tariff


@pytest.fixture(scope="module")
def schedule():
    return Schedule(load_house())


@pytest.mark.parametrize("day_type", ["weekday", "holiday"])
def test_each_member_accounts_for_every_hour(schedule, day_type):
    # 每位成員每小時的比例加總必須剛好是 1（在某空間或外出）
    log = schedule.activity_log(day_type)
    totals = log.groupby(["member", "hour"])["fraction"].sum()
    assert len(totals) == 4 * 24
    assert (totals.round(6) == 1).all()


def test_weekday_nobody_home_during_work_and_school(schedule):
    home = schedule.people_at_home("weekday")
    assert (home.loc[8:15] == 0).all()
    assert home.loc[16] == 1.0   # 小孩 16:30 到家，各待半小時
    assert home.loc[20] == 4


def test_sleeping_in_own_bedrooms(schedule):
    sleep = schedule.occupancy("weekday", activity="sleep")
    for hour in [0, 3, 6, 23]:
        assert sleep.loc[hour, "master_bedroom"] == 2
        assert sleep.loc[hour, "bedroom_1"] == 1
        assert sleep.loc[hour, "bedroom_2"] == 1
    assert sleep.loc[12].sum() == 0


def test_meals_and_showers(schedule):
    occ = schedule.occupancy("weekday")
    assert occ.loc[19, "kitchen"] == 3       # 19:00–19:45 晚餐：4 人 × 0.75 小時
    assert occ.loc[21, "bathroom_1"] > 0      # 家長洗澡
    assert occ.loc[20, "bathroom_2"] > 0      # 小孩洗澡
    assert occ.loc[3, "kitchen"] == 0


def test_holiday_more_time_at_home(schedule):
    weekday = schedule.people_at_home("weekday").sum()
    holiday = schedule.people_at_home("holiday").sum()
    assert holiday > weekday
    assert schedule.people_at_home("holiday").loc[15] == 0  # 假日下午外出


def test_utility_space_has_no_occupants(schedule):
    assert schedule.occupancy("holiday")["utility"].sum() == 0


def test_day_type_uses_holiday_calendar():
    tariff = Tariff()
    assert day_type_for(date(2026, 7, 20), tariff.is_off_peak_day) == "weekday"
    assert day_type_for(date(2026, 7, 18), tariff.is_off_peak_day) == "holiday"   # 週六
    assert day_type_for(date(2026, 10, 10), tariff.is_off_peak_day) == "holiday"  # 國慶日


def test_invalid_space_rejected(tmp_path):
    config = json.loads((CONFIG_DIR / "schedule.json").read_text(encoding="utf-8"))
    config["routines"]["adult_weekday"]["blocks"].append(
        {"start": "20:00", "end": "21:00", "space": "garage", "activity": "leisure"}
    )
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="garage"):
        Schedule(load_house(), path)
