"""由內政部「鄉鎮市區界線（TWD97 經緯度）」SHP 計算各鄉鎮市區中心點，輸出 config/towns.json（第 6b 批）。

用法：python scripts/build_town_centroids.py <TOWN_MOI_xxxxxxx.shp 的路徑>
資料來源：https://data.gov.tw/dataset/7441（內政部國土測繪中心，政府資料開放授權條款第 1 版）

只用標準函式庫讀 SHP（多邊形）與 DBF（屬性），不需要 geopandas。
中心點取「面積最大的那一塊多邊形」的形心：離島多的鄉鎮（例如澎湖、金門）才不會落在海上。
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "config" / "towns.json"
# 縣市排序：北到南、離島最後（與一般選單習慣相同）
COUNTY_ORDER = ["臺北市", "新北市", "基隆市", "桃園市", "新竹市", "新竹縣", "苗栗縣", "臺中市", "彰化縣", "南投縣",
                "雲林縣", "嘉義市", "嘉義縣", "臺南市", "高雄市", "屏東縣", "宜蘭縣", "花蓮縣", "臺東縣",
                "澎湖縣", "金門縣", "連江縣"]


def read_dbf(path: Path) -> list[dict[str, str]]:
    data = path.read_bytes()
    n_records, header_len, record_len = struct.unpack("<IHH", data[4:12])
    fields, pos = [], 32
    while data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b"\0")[0].decode("ascii")
        fields.append((name, data[pos + 16]))
        pos += 32
    encoding = "utf-8"
    cpg = path.with_suffix(".CPG")
    if cpg.exists() and "950" in cpg.read_text(errors="ignore"):
        encoding = "cp950"
    rows = []
    for i in range(n_records):
        rec = data[header_len + i * record_len: header_len + (i + 1) * record_len]
        off, row = 1, {}                                   # 第 1 個位元組是刪除標記
        for name, length in fields:
            row[name] = rec[off:off + length].decode(encoding, errors="replace").strip()
            off += length
        rows.append(row)
    return rows


def read_polygons(path: Path) -> list[list[list[tuple[float, float]]]]:
    """每筆紀錄 → 多個環（ring），每個環是 (經度, 緯度) 點列。"""
    data = path.read_bytes()
    pos, shapes = 100, []
    while pos < len(data):
        _, length = struct.unpack(">II", data[pos:pos + 8])
        content = data[pos + 8: pos + 8 + length * 2]
        pos += 8 + length * 2
        if struct.unpack("<i", content[:4])[0] == 0:       # 空的圖形
            shapes.append([])
            continue
        n_parts, n_points = struct.unpack("<ii", content[36:44])
        parts = list(struct.unpack(f"<{n_parts}i", content[44:44 + 4 * n_parts]))
        pts_off = 44 + 4 * n_parts
        coords = struct.unpack(f"<{2 * n_points}d", content[pts_off:pts_off + 16 * n_points])
        points = list(zip(coords[0::2], coords[1::2]))
        shapes.append([points[a:b] for a, b in zip(parts, parts[1:] + [n_points])])
    return shapes


def ring_centroid(ring: list[tuple[float, float]]) -> tuple[float, float, float]:
    """多邊形形心（鞋帶公式），回傳 (面積絕對值, 經度, 緯度)。"""
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-12:
        xs, ys = zip(*ring)
        return 0.0, sum(xs) / len(xs), sum(ys) / len(ys)
    return abs(a) / 2, cx / (3 * a), cy / (3 * a)


def build(shp: Path) -> dict[str, dict[str, list[float]]]:
    rows, shapes = read_dbf(shp.with_suffix(".dbf")), read_polygons(shp)
    towns: dict[str, dict[str, list[float]]] = {}
    for row, rings in zip(rows, shapes):
        if not rings:
            continue
        _, lon, lat = max((ring_centroid(r) for r in rings), key=lambda t: t[0])
        county = row["COUNTYNAME"].replace("台", "臺")
        towns.setdefault(county, {})[row["TOWNNAME"]] = [round(lat, 4), round(lon, 4)]
    order = {c: i for i, c in enumerate(COUNTY_ORDER)}
    return {c: dict(sorted(towns[c].items())) for c in sorted(towns, key=lambda c: order.get(c, 99))}


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    towns = build(Path(sys.argv[1]))
    OUT.write_text(json.dumps({"_source": "內政部國土測繪中心 鄉鎮市區界線（TWD97 經緯度）1140318；"
                                          "中心點為面積最大多邊形的形心",
                               "towns": towns}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(towns)} 個縣市、{sum(len(t) for t in towns.values())} 個鄉鎮市區 → {OUT}")


if __name__ == "__main__":
    main()
