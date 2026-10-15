"""階段 1：資料結構與標配房型讀取。"""
import json

import pytest

from core.models import CONFIG_DIR, LoadType, load_house


@pytest.fixture
def house():
    return load_house()


def test_default_house_spaces(house):
    # 3 房 + 2 衛 + 客廳 + 廚房 + 公共設備
    assert len(house.spaces) == 8
    assert house.get_space("master_bedroom").area_ping == 5
    assert house.total_area_ping == 5 + 4 + 3.5 + 8 + 3 + 1.5 + 1.5


def test_room_differences_from_template(house):
    # 次臥 1 多了電腦；浴室 2 沒有暖風機
    assert house.get_space("bedroom_1").get_appliance("computer") is not None
    assert house.get_space("bedroom_2").get_appliance("computer") is None
    assert house.get_space("bathroom_2").get_appliance("bath_heater") is None
    assert house.get_space("bathroom_1").get_appliance("bath_heater") is not None


def test_power_overrides(house):
    # 房屋設定覆寫 > 範本覆寫 > 目錄預設
    assert house.get_space("master_bedroom").get_appliance("air_conditioner").power_w == 1100
    assert house.get_space("living_room").get_appliance("air_conditioner").power_w == 1500
    assert house.get_space("bedroom_2").get_appliance("air_conditioner").power_w == 800


def test_appliances_not_shared(house):
    # 修改一個空間的家電不應影響其他空間
    house.get_space("bedroom_1").get_appliance("lighting").power_w = 999
    assert house.get_space("bedroom_2").get_appliance("lighting").power_w == 30


def test_load_types(house):
    essential = {a.id for _, a in house.all_appliances() if a.load_type is LoadType.ESSENTIAL}
    shiftable = {a.id for _, a in house.all_appliances() if a.load_type is LoadType.SHIFTABLE}
    assert essential == {"refrigerator", "router", "camera"}
    assert shiftable == {"washer", "dryer", "water_heater"}


def test_count_and_invalid_template(tmp_path):
    house_file = tmp_path / "house.json"
    house_file.write_text(json.dumps({
        "name": "測試", "occupants": 2,
        "spaces": [{"id": "room", "template": "bedroom", "count": 3}],
    }), encoding="utf-8")
    house = load_house(house_file)
    assert [s.name for s in house.spaces] == ["臥室 1", "臥室 2", "臥室 3"]
    assert house.spaces[0].area_ping == 4  # 範本預設面積

    house_file.write_text(json.dumps({
        "name": "測試", "occupants": 2,
        "spaces": [{"id": "x", "template": "garage"}],
    }), encoding="utf-8")
    with pytest.raises(ValueError):
        load_house(house_file)


def test_config_dir_exists():
    assert (CONFIG_DIR / "default_house.json").exists()
