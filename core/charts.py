"""靜態圖表（matplotlib，輸出 PNG 供報告與 README 使用）。互動圖表會在階段 8 的 Streamlit 製作。

設計原則：
- 顏色跟著「用電分類」固定，不隨排序改變；四色依序取自經色盲驗證的類別色盤。
- 單一 y 軸；不同單位（用電、氣溫）分成上下兩張小圖，不用雙軸。
- 尖峰時段用淺灰底色標示，不佔用類別顏色。
- 細格線、相鄰色塊之間留白線區隔；每張圖都有對應的 CSV 表格可查數值。
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 不開視窗，直接存檔
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from core.simulator import CATEGORIES, MonthResult, SimulationResult  # noqa: E402

# 類別色盤（依序使用，不循環）與介面墨色
SERIES = {"冷氣": "#2a78d6", "熱水與洗烘": "#eb6834", "其他家電": "#1baf7a", "必要負載": "#eda100"}
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
PERIOD_FILL = {"尖峰": "#ecebe6", "半尖峰": "#f5f4f0"}

plt.rcParams.update({
    "font.sans-serif": ["Microsoft JhengHei", "Noto Sans TC", "Noto Sans CJK TC", "PingFang TC", "sans-serif"],
    "axes.unicode_minus": False,
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "axes.edgecolor": AXIS,
    "axes.labelcolor": INK_2,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "text.color": INK,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.6,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "font.size": 10,
})


def _shade_periods(ax, hourly) -> None:
    """依電價時段在背景加淺灰底色，並在上緣標示費率。"""
    labels = list(hourly["period_label"])
    prices = list(hourly["price"])
    h = 0
    while h < 24:
        end = h
        while end + 1 < 24 and labels[end + 1] == labels[h]:
            end += 1
        if labels[h] in PERIOD_FILL:
            ax.axvspan(h - 0.5, end + 0.5, color=PERIOD_FILL[labels[h]], zorder=0, linewidth=0)
        ax.text((h + end) / 2, 1.01, f"{labels[h]} {prices[h]:.2f} 元/度", transform=ax.get_xaxis_transform(),
                ha="center", va="bottom", fontsize=8, color=MUTED)
        h = end + 1


def plot_day(result: SimulationResult, path: Path) -> Path:
    """一天的全屋逐時用電（依分類堆疊）＋ 各空間用電排名。"""
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(11, 8.5), gridspec_kw={"height_ratios": [3, 2], "hspace": 0.45})

    # 上圖：逐時堆疊長條
    data = result.hourly_by_category()
    bottom = [0.0] * 24
    for cat in CATEGORIES:
        values = list(data[cat])
        ax.bar(range(24), values, bottom=bottom, width=0.8, color=SERIES[cat], label=cat,
               edgecolor=SURFACE, linewidth=1.0, zorder=3)
        bottom = [b + v for b, v in zip(bottom, values)]
    _shade_periods(ax, result.hourly)
    peak = result.peak_hour
    ax.annotate(f"最大 {result.peak_demand_kw:.2f} kWh", xy=(peak, bottom[peak]),
                xytext=(0, 6), textcoords="offset points", ha="center", fontsize=9, color=INK_2)
    ax.set_xlim(-0.6, 23.6)
    ax.set_ylim(0, max(bottom) * 1.18)
    ax.set_xticks(range(24), [f"{h:02d}" for h in range(24)])
    ax.set_xlabel("時")
    ax.set_ylabel("用電（kWh）")
    ax.grid(axis="x", visible=False)
    day_name = "平日" if result.day_type == "weekday" else "假日"
    ax.set_title(f"{result.day}（{day_name}）全屋逐時用電｜{result.control_name}\n"
                 f"共 {result.total_kwh:.1f} kWh，流動電費 {result.energy_cost:.0f} 元",
                 loc="left", fontsize=12, pad=40)
    # 圖例放在繪圖區上方（時段標示之上），避免蓋住長條
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.06), ncols=4, frameon=False, fontsize=9)

    # 下圖：各空間用電，同樣依分類堆疊（顏色意義和上圖一致）
    by_space = (result.records.pivot_table(index="space", columns="category", values="kwh", aggfunc="sum")
                .reindex(columns=CATEGORIES, fill_value=0.0).fillna(0.0))
    totals = by_space.sum(axis=1).sort_values()
    by_space = by_space.loc[totals.index]
    left = [0.0] * len(by_space)
    for cat in CATEGORIES:
        values = list(by_space[cat])
        ax2.barh(by_space.index, values, left=left, height=0.6, color=SERIES[cat],
                 edgecolor=SURFACE, linewidth=1.0, zorder=3)
        left = [a + b for a, b in zip(left, values)]
    for i, v in enumerate(totals.values):
        ax2.text(v + totals.max() * 0.01, i, f"{v:.2f} kWh（{v / result.total_kwh:.0%}）",
                 va="center", fontsize=8.5, color=INK_2)
    ax2.set_xlim(0, totals.max() * 1.3)
    ax2.set_xlabel("用電（kWh）")
    ax2.grid(axis="y", visible=False)
    ax2.set_title("各空間用電（可移轉家電計入公共設備；顏色同上圖分類）", loc="left", fontsize=11)

    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


MODE_COLORS = {"基準（一般習慣）": INK_2, "省電模式": SERIES["冷氣"], "正常模式": SERIES["熱水與洗烘"],
               "效能模式": SERIES["其他家電"], "舒眠模式": SERIES["必要負載"]}


def plot_modes(table, results: dict, path: Path) -> Path:
    """四種模式比較：逐時用電曲線（上）＋ 流動電費、舒適度（下方兩張小圖）。
    基準情境用中性灰，四種模式依序取類別色；顏色跟著模式走。"""
    fig = plt.figure(figsize=(11, 8.5))
    grid = fig.add_gridspec(2, 2, height_ratios=[3, 2], hspace=0.5, wspace=0.3)
    ax = fig.add_subplot(grid[0, :])
    ax_cost = fig.add_subplot(grid[1, 0])
    ax_comfort = fig.add_subplot(grid[1, 1])

    first = None
    for key, r in results.items():
        sim_result = r if isinstance(r, SimulationResult) else r.result
        first = first or sim_result
        name = table.loc[list(results).index(key), "模式"]
        ax.plot(range(24), sim_result.hourly["kwh"], color=MODE_COLORS[name], linewidth=2,
                label=name, zorder=3, marker="o", markersize=3)
    _shade_periods(ax, first.hourly)
    ax.set_xlim(-0.5, 23.5)
    ax.set_ylim(0, None)
    ax.set_xticks(range(24), [f"{h:02d}" for h in range(24)])
    ax.set_xlabel("時")
    ax.set_ylabel("用電（kWh）")
    day_name = "平日" if first.day_type == "weekday" else "假日"
    ax.set_title(f"{first.day}（{day_name}）四種模式逐時用電比較", loc="left", fontsize=12, pad=40)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.06), ncols=5, frameon=False, fontsize=9)

    names = list(table["模式"])
    colors = [MODE_COLORS[n] for n in names]
    short = [n.replace("（一般習慣）", "").replace("模式", "") for n in names]
    for axis, column, fmt, title in (
        (ax_cost, "流動電費(元)", "{:.0f} 元", "流動電費（元）"),
        (ax_comfort, "平均溫差(°C)", "{:.2f}°C", "平均溫差（越小越舒適）"),
    ):
        values = list(table[column])
        bars = axis.bar(short, values, color=colors, width=0.6, zorder=3)
        for bar, v in zip(bars, values):
            axis.annotate(fmt.format(v), xy=(bar.get_x() + bar.get_width() / 2, v), xytext=(0, 3),
                          textcoords="offset points", ha="center", va="bottom", fontsize=8.5, color=INK_2)
        axis.set_ylim(0, max(values) * 1.2 if max(values) > 0 else 1)
        axis.grid(axis="x", visible=False)
        axis.set_title(title, loc="left", fontsize=11)

    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_month(result: MonthResult, path: Path) -> Path:
    """整月每日用電（平日／假日）＋ 每日平均氣溫（上下兩張小圖，共用日期軸）。"""
    daily = result.daily()
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1.3], "hspace": 0.15})
    colors = [SERIES["冷氣"] if t == "weekday" else SERIES["熱水與洗烘"] for t in daily["day_type"]]
    days = [d.day for d in daily["date"]]
    ax.bar(days, daily["kwh"], color=colors, width=0.75, zorder=3)
    ax.set_ylabel("日用電（kWh）")
    ax.grid(axis="x", visible=False)
    first = daily["date"].iloc[0]
    ax.set_title(f"{first.year} 年 {first.month} 月每日用電｜基準情境\n"
                 f"月用電 {result.total_kwh:.0f} kWh，日平均 {daily.kwh.mean():.1f} kWh",
                 loc="left", fontsize=12, pad=24)
    # 圖例放在繪圖區上方，避免蓋住長條
    ax.legend(handles=[Patch(color=SERIES["冷氣"], label="平日"), Patch(color=SERIES["熱水與洗烘"], label="假日")],
              loc="lower left", bbox_to_anchor=(0, 1.0), ncols=2, frameon=False, fontsize=9)

    ax2.plot(days, daily["temp_mean_c"], color=INK_2, linewidth=2, marker="o", markersize=3)
    ax2.set_ylabel("日均溫（°C）")
    ax2.set_xlabel("日")
    ax2.set_xticks(days[::2])
    ax2.grid(axis="x", visible=False)

    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path
