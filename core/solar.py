"""太陽位置與各方位牆面日射（優化 A3：窗戶方位與西曬）。

氣象站只量「水平面全天空日射量」（GHI）。牆與窗是垂直面，照到多少陽光取決於太陽位置與面向：
1. 太陽位置：依日期、時間、經緯度算出太陽高度角與方位角（Cooper 赤緯式 + 均時差）
2. 直射／散射分離：Erbs 相關式，由晴空指數 kt = GHI ÷ 大氣層外水平日射，估算散射比例
3. 垂直面日射 = 直射 × cos(入射角) + 天空散射 × 1/2（等向性天空）+ 地面反射 × 反照率 × 1/2

方位以「牆面朝向」表示：N 北、E 東、S 南、W 西（朝西的窗下午被直射＝西曬）。
"""
from __future__ import annotations

import math
from datetime import date

SOLAR_CONSTANT = 1367.0   # W/m²
MIN_BEAM_ALTITUDE = 5.0   # 太陽高度低於此值（日出日落那一小時）全部視為散射：
                          # 逐時資料是整小時累積，用小時中點的低太陽高度換算直射會被過度放大
MAX_DNI = 1000.0          # 地表法線直射日射上限（W/m²，晴朗海平面約 900–1000）
ORIENTATIONS = {"N": 0, "NE": 45, "E": 90, "SE": 135, "S": 180, "SW": 225, "W": 270, "NW": 315}
ORIENTATION_NAMES = {"N": "北", "NE": "東北", "E": "東", "SE": "東南", "S": "南", "SW": "西南", "W": "西", "NW": "西北"}


def solar_position(day: date, hour: float, lat: float, lon: float, tz_h: float = 8.0) -> tuple[float, float]:
    """某地某時刻的太陽（高度角, 方位角），單位度；方位角由正北順時針（東 90、南 180、西 270）。
    hour 為當地標準時間（可帶小數，例如 13.5 代表 13:30）。"""
    n = day.timetuple().tm_yday
    b = math.radians(360 * (n - 81) / 364)
    eot_min = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)    # 均時差（分鐘）
    solar_time = hour + (4 * (lon - 15 * tz_h) + eot_min) / 60
    omega = math.radians(15 * (solar_time - 12))                                 # 時角，上午為負
    delta = math.radians(23.45 * math.sin(math.radians(360 * (284 + n) / 365)))  # 赤緯
    phi = math.radians(lat)
    sin_alt = math.sin(phi) * math.sin(delta) + math.cos(phi) * math.cos(delta) * math.cos(omega)
    alt = math.asin(max(-1.0, min(1.0, sin_alt)))
    cos_az = (math.sin(delta) - sin_alt * math.sin(phi)) / max(1e-9, math.cos(alt) * math.cos(phi))
    az = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
    if omega > 0:              # 下午太陽在西半邊
        az = 360 - az
    return math.degrees(alt), az


def extraterrestrial_horizontal(day: date, altitude_deg: float) -> float:
    """大氣層外水平面日射（W/m²）。"""
    n = day.timetuple().tm_yday
    g0 = SOLAR_CONSTANT * (1 + 0.033 * math.cos(math.radians(360 * n / 365)))
    return max(0.0, g0 * math.sin(math.radians(altitude_deg)))


def split_beam_diffuse(ghi: float, g0h: float) -> tuple[float, float]:
    """Erbs 相關式：水平全天空日射 → （水平直射, 水平散射）W/m²。"""
    if ghi <= 0 or g0h <= 0:
        return 0.0, max(0.0, ghi)
    kt = min(ghi / g0h, 1.0)
    if kt <= 0.22:
        fd = 1 - 0.09 * kt
    elif kt <= 0.80:
        fd = 0.9511 - 0.1604 * kt + 4.388 * kt ** 2 - 16.638 * kt ** 3 + 12.336 * kt ** 4
    else:
        fd = 0.165
    diffuse = ghi * fd
    return ghi - diffuse, diffuse


def vertical_irradiance(ghi: float, altitude_deg: float, azimuth_deg: float, g0h: float,
                        facing_deg: float, albedo: float = 0.2) -> float:
    """朝向 facing_deg 的垂直面所受日射（W/m²）。"""
    if ghi <= 0:
        return 0.0
    beam_h, diffuse_h = split_beam_diffuse(ghi, g0h)
    alt = math.radians(max(altitude_deg, 0.0))
    if altitude_deg <= MIN_BEAM_ALTITUDE:
        diffuse_h, beam_h = ghi, 0.0
    beam = 0.0
    if altitude_deg > MIN_BEAM_ALTITUDE:
        dni = min(beam_h / math.sin(alt), MAX_DNI)
        cos_inc = math.cos(alt) * math.cos(math.radians(azimuth_deg - facing_deg))
        beam = dni * max(0.0, cos_inc)
    return beam + diffuse_h * 0.5 + ghi * albedo * 0.5


def hourly_vertical(day: date, ghi_w_m2: list[float], lat: float, lon: float, orientation: str | None,
                    tz_h: float = 8.0, albedo: float = 0.2) -> list[float]:
    """24 小時某方位垂直面的平均日射（W/m²）。每小時取該小時中點（h + 0.5）的太陽位置。
    orientation 為 None（未指定方位）時，取東南西北四面的平均。"""
    return hourly_vertical_many(day, ghi_w_m2, lat, lon, [orientation], tz_h, albedo)[orientation]


def hourly_vertical_many(day: date, ghi_w_m2: list[float], lat: float, lon: float, orientations: list[str | None],
                         tz_h: float = 8.0, albedo: float = 0.2) -> dict[str | None, list[float]]:
    """一次算多個方位：同一小時的太陽位置、直射散射分離只算一次，再套用到各方位（比逐方位計算快數倍）。"""
    facing_sets = {o: [ORIENTATIONS[o]] if o else [0, 90, 180, 270] for o in orientations}
    unique = sorted({f for fs in facing_sets.values() for f in fs})
    result: dict[str | None, list[float]] = {o: [] for o in orientations}
    for h, ghi in enumerate(ghi_w_m2):
        if ghi <= 0:
            for o in orientations:
                result[o].append(0.0)
            continue
        alt, az = solar_position(day, h + 0.5, lat, lon, tz_h)
        g0h = extraterrestrial_horizontal(day, alt)
        per_facing = {f: vertical_irradiance(ghi, alt, az, g0h, f, albedo) for f in unique}
        for o, fs in facing_sets.items():
            result[o].append(sum(per_facing[f] for f in fs) / len(fs))
    return result
