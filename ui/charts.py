"""互動圖表（Altair，Streamlit 內建）。顏色與 core/charts.py 的靜態圖一致：
用電分類與模式各自固定顏色，不隨排序或篩選改變；尖峰時段以淺灰底標示；每張圖都有滑鼠提示。"""
from __future__ import annotations

import altair as alt
import pandas as pd

from core.simulator import CATEGORIES, SimulationResult

CATEGORY_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]   # 冷氣、熱水與洗烘、其他家電、必要負載
MODE_NAMES = ["基準（一般習慣）", "省電模式", "正常模式", "效能模式", "舒眠模式"]
MODE_COLORS = ["#6f6e69", "#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
PERIOD_SHADE = {"尖峰": 0.16, "半尖峰": 0.08}
HOURS = list(range(24))


def _category_scale() -> alt.Scale:
    return alt.Scale(domain=CATEGORIES, range=CATEGORY_COLORS)


def _mode_scale(names: list[str]) -> alt.Scale:
    colors = dict(zip(MODE_NAMES, MODE_COLORS))
    return alt.Scale(domain=names, range=[colors.get(n, "#6f6e69") for n in names])


def _period_layer(hourly: pd.DataFrame) -> alt.Chart:
    """尖峰、半尖峰時段的灰色底（每小時一格），滑鼠移上去顯示電價。"""
    df = hourly[["hour", "period_label", "price"]].copy()
    df = df[df["period_label"].isin(PERIOD_SHADE)]
    df["start"], df["end"] = df["hour"] - 0.5, df["hour"] + 0.5
    df["opacity"] = df["period_label"].map(PERIOD_SHADE)
    return alt.Chart(df).mark_rect(color="#8a8a8a").encode(
        x=_hour_axis(field="start"), x2="end:Q", opacity=alt.Opacity("opacity:Q", legend=None, scale=None),
        tooltip=[alt.Tooltip("period_label:N", title="時段"), alt.Tooltip("price:Q", title="電價（元/度）")])


def _hour_axis(title: str = "時", field: str = "hour") -> alt.X:
    return alt.X(f"{field}:Q", title=title, scale=alt.Scale(domain=[-0.5, 23.5], nice=False),
                 axis=alt.Axis(values=HOURS, tickMinStep=1, grid=False, labelExpr="datum.value"))


PERIOD_COLORS = {"尖峰": "#eb6834", "半尖峰": "#eda100", "離峰": "#1baf7a", "不分時段": "#2a78d6"}


def price_bars(prices: pd.DataFrame) -> alt.Chart:
    """24 小時電價長條：顏色代表時段（尖峰／半尖峰／離峰），電價說明視窗用。"""
    present = [p for p in PERIOD_COLORS if p in set(prices["時段"])]
    data = prices[["hour", "price", "時段"]].astype({"hour": int, "price": float})
    bars = alt.Chart(data).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
        x=alt.X("hour:O", title="時", axis=alt.Axis(labelAngle=0)),
        y=alt.Y("price:Q", title="元/度", scale=alt.Scale(domain=[0, 8])),
        color=alt.Color("時段:N", scale=alt.Scale(domain=present, range=[PERIOD_COLORS[p] for p in present]),
                        legend=alt.Legend(orient="top", title=None)),
        tooltip=[alt.Tooltip("hour:O", title="時"), "時段:N", alt.Tooltip("price:Q", title="元/度", format=".2f")],
    )
    labels = bars.mark_text(dy=-7, fontSize=10, color="#6f6e69").encode(text=alt.Text("price:Q", format=".2f"),
                                                                        color=alt.value("#6f6e69"))
    return (bars + labels).properties(height=280)


def forecast_daily(daily: pd.DataFrame) -> alt.Chart:
    """預測頁：每日預測用電（實線，含使用紀錄與帳單修正）與純物理模擬（虛線）。daily：date、kwh、sim_kwh。"""
    data = daily[["date", "kwh", "sim_kwh"]].rename(columns={"kwh": "預測", "sim_kwh": "物理模擬"})
    data["date"] = pd.to_datetime(data["date"])
    long = data.melt("date", var_name="種類", value_name="kWh")
    scale = alt.Scale(domain=["預測", "物理模擬"], range=["#2a78d6", "#8a94a6"])
    return alt.Chart(long).mark_line(point=alt.OverlayMarkDef(size=18)).encode(
        x=alt.X("date:T", title="日期", axis=alt.Axis(format="%m/%d")),
        y=alt.Y("kWh:Q", title="每日用電（度）"),
        color=alt.Color("種類:N", scale=scale, legend=alt.Legend(orient="top", title=None)),
        strokeDash=alt.StrokeDash("種類:N", scale=alt.Scale(domain=["預測", "物理模擬"], range=[[1, 0], [4, 3]]),
                                  legend=None),
        tooltip=[alt.Tooltip("date:T", title="日期", format="%Y-%m-%d"), "種類:N",
                 alt.Tooltip("kWh:Q", title="度", format=".1f")],
    ).properties(height=280)


def month_daily_lines(daily: pd.DataFrame, field: str = "kwh", title: str = "每日用電（度）") -> alt.Chart:
    """整月分析：各模式每天的用電（或累積電費 cum_cost）折線。daily 為 ui.insights.daily_table() 的長表格。"""
    names = list(dict.fromkeys(daily["模式"]))
    data = daily[["date", "模式", field, "temp_mean_c"]].copy()
    data["date"] = pd.to_datetime(data["date"])
    fmt = ".0f" if field == "cum_cost" else ".1f"
    return alt.Chart(data).mark_line(point=alt.OverlayMarkDef(size=14), strokeWidth=2).encode(
        x=alt.X("date:T", title="日期", axis=alt.Axis(format="%m/%d")),
        y=alt.Y(f"{field}:Q", title=title),
        color=alt.Color("模式:N", scale=_mode_scale(names), sort=names, legend=alt.Legend(orient="top", title=None)),
        tooltip=[alt.Tooltip("date:T", title="日期", format="%Y-%m-%d"), "模式:N",
                 alt.Tooltip(f"{field}:Q", title=title, format=fmt),
                 alt.Tooltip("temp_mean_c:Q", title="日均溫（°C）", format=".1f")],
    ).properties(height=300)


def temp_vs_ac(daily: pd.DataFrame) -> alt.LayerChart:
    """整月分析：室外日均溫與冷氣用電的關係（每點一天），每個模式一條迴歸線。"""
    names = list(dict.fromkeys(daily["模式"]))
    data = daily[["date", "模式", "temp_mean_c", "ac_kwh"]].copy()
    data["date"] = pd.to_datetime(data["date"])
    color = alt.Color("模式:N", scale=_mode_scale(names), sort=names, legend=alt.Legend(orient="top", title=None))
    points = alt.Chart(data).mark_circle(size=42, opacity=0.75).encode(
        x=alt.X("temp_mean_c:Q", title="室外日均溫（°C）", scale=alt.Scale(zero=False)),
        y=alt.Y("ac_kwh:Q", title="冷氣用電（度／天）"), color=color,
        tooltip=[alt.Tooltip("date:T", title="日期", format="%Y-%m-%d"), "模式:N",
                 alt.Tooltip("temp_mean_c:Q", title="日均溫（°C）", format=".1f"),
                 alt.Tooltip("ac_kwh:Q", title="冷氣（度）", format=".2f")])
    lines = alt.Chart(data).transform_regression("temp_mean_c", "ac_kwh", groupby=["模式"]).mark_line(
        strokeDash=[4, 3], opacity=0.8).encode(x="temp_mean_c:Q", y="ac_kwh:Q", color=color)
    return (points + lines).properties(height=300)


def whatif_bars(table: pd.DataFrame) -> alt.Chart:
    """情境比較：各情境每年省下的電費（長條），滑鼠提示投資金額與回收年限。"""
    data = table[["情境", "每年省(元)", "投資(元)", "回收年限(年)", "使用年限內淨效益(元)"]].copy()
    data["回收"] = data["回收年限(年)"].map(lambda y: "—" if pd.isna(y) else f"{y:.1f} 年")
    return alt.Chart(data).mark_bar(cornerRadiusEnd=4, color="#1baf7a").encode(
        y=alt.Y("情境:N", sort="-x", title=None),
        x=alt.X("每年省(元):Q", title="每年省下的電費（元）"),
        tooltip=["情境:N", alt.Tooltip("每年省(元):Q", format=",.0f"), alt.Tooltip("投資(元):Q", format=",.0f"),
                 alt.Tooltip("回收:N", title="回收年限"),
                 alt.Tooltip("使用年限內淨效益(元):Q", format=",.0f")],
    ).properties(height=max(120, 46 * len(data)))


def anomaly_chart(scored: pd.DataFrame, k: float) -> alt.LayerChart:
    """異常偵測：一個通道逐時「實測 − 模擬」的誤差；灰帶為正常範圍（中位數 ± kσ），紅點為異常。"""
    data = scored[["date", "hour", "measured_kwh", "simulated_kwh", "residual", "center", "scale", "z"]].copy()
    data["time"] = pd.to_datetime(data["date"].astype(str)) + pd.to_timedelta(data["hour"], unit="h")
    data["lo"], data["hi"] = data["center"] - k * data["scale"], data["center"] + k * data["scale"]
    data["異常"] = data["z"].abs() > k
    x = alt.X("time:T", title="時間", axis=alt.Axis(format="%m/%d %H時"))
    tooltip = [alt.Tooltip("time:T", title="時間", format="%m/%d %H:00"),
               alt.Tooltip("measured_kwh:Q", title="實測（kWh）", format=".2f"),
               alt.Tooltip("simulated_kwh:Q", title="模擬（kWh）", format=".2f"),
               alt.Tooltip("residual:Q", title="誤差（kWh）", format="+.2f"),
               alt.Tooltip("z:Q", title="偏離（σ）", format="+.1f")]
    band = alt.Chart(data).mark_area(color="#8a94a6", opacity=0.18).encode(x=x, y="lo:Q", y2="hi:Q")
    line = alt.Chart(data).mark_line(color="#2a78d6", strokeWidth=1.4).encode(
        x=x, y=alt.Y("residual:Q", title="實測 − 模擬（kWh）"), tooltip=tooltip)
    points = alt.Chart(data[data["異常"]]).mark_circle(color="#eb6834", size=70).encode(
        x=x, y="residual:Q", tooltip=tooltip)
    return (band + line + points).properties(height=300)


def hourly_stacked(result: SimulationResult) -> alt.LayerChart:
    """全屋逐時用電，依用電分類堆疊；灰底為尖峰時段。"""
    wide = result.hourly_by_category()
    top = wide.cumsum(axis=1)                      # 依分類順序累加，算出每段的上緣
    rows = []
    for h in HOURS:
        for cat in CATEGORIES:
            kwh = float(wide.loc[h, cat])
            if kwh > 0:
                rows.append({"hour": h, "分類": cat, "kWh": kwh, "y0": float(top.loc[h, cat]) - kwh,
                             "y1": float(top.loc[h, cat]), "x0": h - 0.4, "x1": h + 0.4})
    data = pd.DataFrame(rows).merge(result.hourly[["hour", "price", "period_label", "kwh"]], on="hour")
    bars = alt.Chart(data).mark_rect(stroke="white", strokeWidth=0.8).encode(
        x=_hour_axis(field="x0"), x2="x1:Q",
        y=alt.Y("y0:Q", title="用電（kWh）"), y2="y1:Q",
        color=alt.Color("分類:N", scale=_category_scale(), legend=alt.Legend(orient="top", title=None)),
        tooltip=[alt.Tooltip("hour:Q", title="時"), alt.Tooltip("分類:N"),
                 alt.Tooltip("kWh:Q", format=".2f"), alt.Tooltip("kwh:Q", title="該小時合計", format=".2f"),
                 alt.Tooltip("period_label:N", title="時段"), alt.Tooltip("price:Q", title="電價（元/度）")])
    return (_period_layer(result.hourly) + bars).properties(height=320)


def today_curve(result: SimulationResult, baseline: SimulationResult, hour: int) -> alt.LayerChart:
    """首頁的今日用電曲線：目前模式（漸層面積）、一般習慣（灰線）、「現在」標線、尖峰灰底。"""
    data = pd.DataFrame({"hour": HOURS, "目前模式": result.hourly["kwh"].values,
                         "一般習慣": baseline.hourly["kwh"].values, "電價": result.hourly["price"].values,
                         "時段": result.hourly["period_label"].values})
    x = _hour_axis()
    area = alt.Chart(data).mark_area(
        interpolate="monotone", line={"color": "#3ba7ff", "strokeWidth": 2},
        color=alt.Gradient(gradient="linear", x1=1, x2=1, y1=1, y2=0,
                           stops=[alt.GradientStop(color="rgba(59,167,255,0.02)", offset=0),
                                  alt.GradientStop(color="rgba(59,167,255,0.45)", offset=1)]),
    ).encode(x=x, y=alt.Y("目前模式:Q", title="kWh"))
    base_line = alt.Chart(data).mark_line(interpolate="monotone", color="#8a94a6", strokeWidth=1.5,
                                          strokeDash=[4, 3]).encode(x=x, y="一般習慣:Q")
    now = pd.DataFrame({"hour": [hour], "label": ["現在"]})
    rule = alt.Chart(now).mark_rule(color="#22d3ee", strokeWidth=1.5, strokeDash=[3, 3]).encode(x="hour:Q")
    label = alt.Chart(now).mark_text(color="#22d3ee", dy=-6, fontSize=11, fontWeight="bold").encode(
        x="hour:Q", y=alt.value(0), text="label:N")
    hover = alt.selection_point(fields=["hour"], nearest=True, on="pointerover", empty=False)
    points = alt.Chart(data).mark_rule(color="#8a94a6").encode(
        x="hour:Q", opacity=alt.condition(hover, alt.value(0.5), alt.value(0)),
        tooltip=[alt.Tooltip("hour:Q", title="時"), alt.Tooltip("目前模式:Q", format=".2f", title="目前模式 kWh"),
                 alt.Tooltip("一般習慣:Q", format=".2f", title="一般習慣 kWh"), "時段:N",
                 alt.Tooltip("電價:Q", format=".2f", title="元/度")]).add_params(hover)
    return (_period_layer(result.hourly) + area + base_line + rule + label + points).properties(height=230)


def space_breakdown(result: SimulationResult) -> alt.Chart:
    """各空間用電，依分類堆疊（顏色意義同逐時圖）。"""
    data = (result.records.groupby(["space", "category"], as_index=False)["kwh"].sum()
            .rename(columns={"space": "空間", "category": "分類"}))
    order = data.groupby("空間")["kwh"].sum().sort_values(ascending=False).index.tolist()
    return alt.Chart(data).mark_bar(stroke="white", strokeWidth=0.8).encode(
        y=alt.Y("空間:N", sort=order, title=None),
        x=alt.X("sum(kwh):Q", title="用電（kWh）"),
        color=alt.Color("分類:N", scale=_category_scale(), legend=alt.Legend(orient="top", title=None)),
        order=alt.Order("分類:N"),
        tooltip=["空間:N", "分類:N", alt.Tooltip("kwh:Q", title="kWh", format=".2f")],
    ).properties(height=44 * len(order) + 110)


def mode_lines(results: dict[str, SimulationResult]) -> alt.LayerChart:
    """各模式逐時用電曲線；滑鼠移動時顯示該小時所有模式的數值（十字線）。"""
    names = list(results)
    data = pd.concat([r.hourly[["hour", "kwh", "price"]].assign(模式=n) for n, r in results.items()])
    hover = alt.selection_point(fields=["hour"], nearest=True, on="pointerover", empty=False)
    base = alt.Chart(data).encode(x=_hour_axis(), color=alt.Color(
        "模式:N", scale=_mode_scale(names), legend=alt.Legend(orient="top", title=None)))
    lines = base.mark_line(strokeWidth=2).encode(y=alt.Y("kwh:Q", title="用電（kWh）"))
    points = base.mark_circle(size=60).encode(
        y="kwh:Q", opacity=alt.condition(hover, alt.value(1), alt.value(0)))
    rule = alt.Chart(data).mark_rule(color="#8a8a8a").encode(
        x="hour:Q", opacity=alt.condition(hover, alt.value(0.6), alt.value(0)),
        tooltip=[alt.Tooltip("hour:Q", title="時")] + [
            alt.Tooltip(f"{n}:Q", title=n, format=".2f") for n in names],
    ).transform_pivot("模式", value="kwh", groupby=["hour"]).add_params(hover)
    first = next(iter(results.values()))
    return (_period_layer(first.hourly) + lines + points + rule).properties(height=320)


def metric_bars(table: pd.DataFrame, metrics: list[tuple[str, str]]) -> alt.VConcatChart:
    """多指標小圖：每張一個指標，長條依模式上色。"""
    names = list(table["模式"])
    short = {n: n.replace("（一般習慣）", "").replace("模式", "") for n in names}
    charts = []
    for column, title in metrics:
        data = table[["模式", column]].rename(columns={column: "值"})
        data["簡稱"] = data["模式"].map(short)
        charts.append(alt.Chart(data, title=alt.Title(title, anchor="start", fontSize=13)).mark_bar().encode(
            x=alt.X("簡稱:N", sort=[short[n] for n in names], title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("值:Q", title=None),
            color=alt.Color("模式:N", scale=_mode_scale(names), legend=None),
            tooltip=["模式:N", alt.Tooltip("值:Q", title=title, format=".2f")],
        ).properties(width=200, height=170))
    rows = [alt.hconcat(*charts[i:i + 3]) for i in range(0, len(charts), 3)]
    return alt.vconcat(*rows)


def appliance_gantt(result: SimulationResult) -> alt.Chart:
    """家電排程甘特圖：每列一台家電，有用電的小時畫一格，顏色為用電分類、深淺為用電量。"""
    data = result.records[result.records["kwh"] > 1e-6].copy()
    data["家電"] = data["space"] + "・" + data["appliance"]
    data["start"], data["end"] = data["hour"], data["hour"] + 1
    order = (data.groupby("家電")["category"].first().map({c: i for i, c in enumerate(CATEGORIES)})
             .sort_values().index.tolist())
    return alt.Chart(data).mark_rect(stroke="white", strokeWidth=0.5).encode(
        x=alt.X("start:Q", title="時", scale=alt.Scale(domain=[0, 24], nice=False),
                axis=alt.Axis(values=list(range(0, 25, 2)))),
        x2="end:Q",
        y=alt.Y("家電:N", sort=order, title=None),
        color=alt.Color("category:N", title="分類", scale=_category_scale(), legend=alt.Legend(orient="top")),
        opacity=alt.Opacity("kwh:Q", scale=alt.Scale(range=[0.35, 1]), legend=None),
        tooltip=["家電:N", alt.Tooltip("hour:Q", title="時"), alt.Tooltip("kwh:Q", title="kWh", format=".3f"),
                 alt.Tooltip("category:N", title="分類")],
    ).properties(height=max(240, 20 * len(order)))


def room_temperature(result: SimulationResult, space_id: str, ideal: list[float], deadband: float) -> alt.LayerChart:
    """某空間的室溫、冷氣設定、室外溫度；灰色帶為舒適範圍（偏好溫度 ± 容許偏差），只在有人時顯示。"""
    climate = result.climate[result.climate["space_id"] == space_id].set_index("hour")
    df = pd.DataFrame({"hour": HOURS, "室溫": climate["room_temp_c"].values,
                       "冷氣設定": climate["setpoint_c"].astype(float).values,
                       "室外": result.hourly["temp_c"].values,
                       "low": [i - deadband if i is not None else None for i in ideal],
                       "high": [i + deadband if i is not None else None for i in ideal]})
    band = alt.Chart(df.dropna(subset=["low"])).mark_area(color="#8a8a8a", opacity=0.18).encode(
        x=_hour_axis(), y=alt.Y("low:Q", title="溫度（°C）", scale=alt.Scale(zero=False)), y2="high:Q",
        tooltip=[alt.Tooltip("low:Q", title="舒適下限", format=".1f"),
                 alt.Tooltip("high:Q", title="舒適上限", format=".1f")])
    long = df.melt("hour", value_vars=["室溫", "冷氣設定", "室外"], var_name="項目", value_name="°C").dropna()
    lines = alt.Chart(long).mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=25)).encode(
        x=_hour_axis(), y=alt.Y("°C:Q", scale=alt.Scale(zero=False)),
        color=alt.Color("項目:N", scale=alt.Scale(domain=["室溫", "冷氣設定", "室外"],
                                                 range=["#2a78d6", "#eb6834", "#8a8a8a"]),
                        legend=alt.Legend(orient="top", title=None)),
        strokeDash=alt.StrokeDash("項目:N", scale=alt.Scale(domain=["室溫", "冷氣設定", "室外"],
                                                          range=[[1, 0], [1, 0], [4, 3]]), legend=None),
        tooltip=[alt.Tooltip("hour:Q", title="時"), "項目:N", alt.Tooltip("°C:Q", format=".1f")])
    return (band + lines).properties(height=300)
