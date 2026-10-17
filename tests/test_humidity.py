"""A4：室內水氣平衡、冷氣開機除濕與除濕機控制。"""
from datetime import date

import pytest

from core.data_source import CwaCsvWeatherSource
from core.load_model import LoadModel, humidity_ratio, relative_humidity
from core.models import load_house
from core.schedule import Schedule

SPRING, SUMMER = date(2026, 3, 10), date(2026, 7, 20)


@pytest.fixture(scope="module")
def model():
    house = load_house()
    return LoadModel(house, Schedule(house))


def _weather(model, day):
    return model.with_solar_geometry(CwaCsvWeatherSource().get_hourly_weather(day), day)


def test_relative_humidity_roundtrip():
    for t, rh in [(26, 55), (30, 80), (18, 40)]:
        assert relative_humidity(t, humidity_ratio(t, rh)) == pytest.approx(rh, abs=0.01)
    assert relative_humidity(20, 0.05) == 100           # 過飽和時上限 100%


def test_room_humidity_physical(model):
    for day in (SPRING, SUMMER):
        for space in model.house.spaces:
            if space.area_ping <= 0:
                continue
            rh = model.space_hourly(space, _weather(model, day), "weekday", 26)["room_rh_pct"]
            assert rh.between(20, 100).all()


def test_shower_raises_bathroom_humidity(model):
    bath = model.house.get_space("bathroom_1")
    df = model.space_hourly(bath, _weather(model, SUMMER), "weekday")   # 夏天不開暖風機，只看洗澡的水氣
    showers = model.schedule.presence("weekday", bath.id, {"shower"})
    shower_hours = [h for h, p in enumerate(showers) if p > 0]
    quiet_hours = [h for h in range(24) if showers[h] == 0 and showers[(h - 1) % 24] == 0]
    assert df["room_rh_pct"][shower_hours].max() > df["room_rh_pct"][quiet_hours].median() + 5
    assert sum(model.activity_moisture(bath, "weekday")) > 0.2       # 洗澡產生的水氣（kg）


def test_ac_startup_dries_room_within_hours(model):
    room = model.house.get_space("bedroom_1")
    on = [0.0] * 18 + [1.0] * 6                         # 18 時開冷氣
    df = model.space_hourly(room, _weather(model, SUMMER), "weekday", 26, ac_on=on)
    assert df["load_latent_pulldown"][18] > 0           # 開機那小時要除掉累積的水氣
    assert df["load_latent_pulldown"][16] == 0
    assert df["room_rh_pct"][21] == pytest.approx(55, abs=3)   # 數小時後維持冷氣的目標濕度
    assert df["room_temp_c"][18:].max() < 26.5          # 除濕只用剩餘能力，不影響降溫


def test_dehumidifier_runs_only_when_humid(model):
    living = model.house.get_space("living_room")
    no_ac = [0.0] * 24
    df = model.space_hourly(living, _weather(model, SPRING), "weekday", ac_on=no_ac)
    running = df["dehumidifier"] > 0
    assert running.any()                                  # 春天潮濕，有開除濕機
    assert (df["dehumidifier"] <= living.get_appliance("dehumidifier").power_w + 1e-9).all()
    off = model.space_hourly(living, _weather(model, SPRING), "weekday", ac_on=no_ac,
                             appliance_factors={"dehumidifier": [0.0] * 24})
    assert df["room_rh_pct"].mean() < off["room_rh_pct"].mean()      # 開除濕機比較乾
    assert df["room_temp_c"].mean() > off["room_temp_c"].mean()      # 除濕機的廢熱讓室溫略升
    dry = model.space_hourly(living, _weather(model, date(2026, 1, 20)), "weekday", ac_on=no_ac)
    assert dry["dehumidifier"].sum() < df["dehumidifier"].sum()       # 乾燥的冬天少開


def test_dehumidifier_remote_override(model):
    living = model.house.get_space("living_room")
    forced = [None] * 24
    forced[3] = 1.0                                       # 半夜沒人也用遙控器開
    df = model.space_hourly(living, _weather(model, SPRING), "weekday", ac_on=[0.0] * 24,
                            appliance_forced={"dehumidifier": forced})
    assert df["dehumidifier"][3] == pytest.approx(living.get_appliance("dehumidifier").power_w)
