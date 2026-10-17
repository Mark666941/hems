"""整月分析報告匯出 PDF（C5）：只用 matplotlib 內建的 PDF 輸出（PdfPages），不需額外套件。

內容（A4 直式）：
1. 封面與摘要：房型、電價方案、各模式月用電／月電費／舒適度比較表、數據分析重點
2. 圖表：各模式每日用電、累積流動電費、室外溫度與冷氣用電（含迴歸線）
3. 節省建議（文字過長時自動換頁）
"""
from __future__ import annotations

import io
import re
import unicodedata
from datetime import date

import matplotlib
import numpy as np
import pandas as pd

import core.charts  # noqa: F401  套用中文字型與圖表樣式（必須在 pyplot 之前設定 Agg）
from ui.charts import MODE_COLORS, MODE_NAMES
from ui.insights import Insight

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402

A4 = (8.27, 11.69)
LEFT, TOP, BOTTOM = 0.08, 0.93, 0.06
WRAP = 46                 # 每行約幾個中文字（英數字算半個）
COLORS = dict(zip(MODE_NAMES, MODE_COLORS))


def _wrap(text: str, width: int) -> list[str]:
    """依顯示寬度換行：中文全形字算 1、英數字算 0.5（中文沒有空白，不能用 textwrap 依空白斷行）。"""
    lines, cur, w = [], "", 0.0
    for ch in text:
        cw = 1.0 if unicodedata.east_asian_width(ch) in "WF" else 0.5
        if w + cw > width and cur:
            lines.append(cur.rstrip())
            cur, w = "", 0.0
            if ch == " ":
                continue
        cur += ch
        w += cw
    if cur:
        lines.append(cur)
    return lines


def _plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


class _TextPages:
    """依序往下寫文字，超出頁面時自動換新的一頁。"""

    def __init__(self, pdf: PdfPages, title: str):
        self.pdf, self.title = pdf, title
        self.fig = None
        self._new_page()

    def _new_page(self) -> None:
        if self.fig is not None:
            self.pdf.savefig(self.fig)
            plt.close(self.fig)
        self.fig = plt.figure(figsize=A4)
        self.y = TOP
        self.fig.text(LEFT, self.y, self.title, fontsize=15, fontweight="bold")
        self.y -= 0.045

    def line(self, text: str, size: float = 10, color: str = "#0b0b0b", bold: bool = False, gap: float = 0.0) -> None:
        for part in _wrap(text, WRAP if size <= 10.5 else int(WRAP * 10 / size)) or [""]:
            if self.y < BOTTOM:
                self._new_page()
            self.fig.text(LEFT, self.y, part, fontsize=size, color=color, fontweight="bold" if bold else "normal")
            self.y -= 0.0021 * size + 0.006
        self.y -= gap

    def table(self, df: pd.DataFrame, height: float) -> None:
        if self.y - height < BOTTOM:
            self._new_page()
        ax = self.fig.add_axes((LEFT, self.y - height, 1 - 2 * LEFT, height))
        ax.axis("off")
        t = ax.table(cellText=df.to_numpy(), colLabels=list(df.columns), loc="upper left", cellLoc="center")
        t.auto_set_font_size(False)
        t.set_fontsize(8.5)
        t.scale(1, 1.35)
        for (r, _), cell in t.get_celld().items():
            cell.set_edgecolor("#c3c2b7")
            if r == 0:
                cell.set_facecolor("#ecebe6")
                cell.set_text_props(fontweight="bold")
        self.y -= height + 0.02

    def close(self) -> None:
        self.pdf.savefig(self.fig)
        plt.close(self.fig)


def _summary_table(mtable: pd.DataFrame, plan_col: str) -> pd.DataFrame:
    cols = {"模式": "模式", "月用電(kWh)": "月用電（度）", plan_col: "月電費（元）",
            "平均舒適時間比例(%)": "舒適時間（%）", "碳排放(kg)": "碳排放（kg）"}
    df = mtable[[c for c in cols if c in mtable.columns]].rename(columns=cols).copy()
    for c in df.columns[1:]:
        df[c] = df[c].map(lambda v: f"{v:,.0f}")
    return df


def _charts_page(pdf: PdfPages, daily: pd.DataFrame, title: str) -> None:
    fig, axes = plt.subplots(3, 1, figsize=A4, gridspec_kw={"hspace": 0.45})
    fig.suptitle(title, x=LEFT, ha="left", fontsize=15, fontweight="bold", y=0.965)
    names = list(dict.fromkeys(daily["模式"]))
    for name in names:
        d = daily[daily["模式"] == name].sort_values("date")
        x = pd.to_datetime(d["date"])
        color = COLORS.get(name, "#6f6e69")
        axes[0].plot(x, d["kwh"], color=color, lw=1.6, marker="o", ms=2.5, label=name)
        axes[1].plot(x, d["cum_cost"], color=color, lw=1.6, label=name)
        axes[2].scatter(d["temp_mean_c"], d["ac_kwh"], color=color, s=12, alpha=0.75, label=name)
        if d["temp_mean_c"].nunique() > 2 and d["ac_kwh"].sum() > 0:
            k, b = np.polyfit(d["temp_mean_c"], d["ac_kwh"], 1)
            xs = np.linspace(d["temp_mean_c"].min(), d["temp_mean_c"].max(), 10)
            axes[2].plot(xs, k * xs + b, color=color, lw=1, ls="--")
    for ax, (t, ylabel) in zip(axes, [("各模式每日用電", "度／天"), ("累積流動電費（不含基本電費）", "元"),
                                      ("室外日均溫與冷氣用電（虛線為迴歸線）", "冷氣 度／天")]):
        ax.set_title(t, loc="left", fontsize=11)
        ax.set_ylabel(ylabel)
    axes[2].set_xlabel("室外日均溫（°C）")
    for ax in axes[:2]:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    axes[0].legend(loc="lower left", bbox_to_anchor=(0, 1.12), ncols=5, frameon=False, fontsize=8)
    pdf.savefig(fig)
    plt.close(fig)


def month_report_pdf(year: int, month: int, house_name: str, plan_name: str, mtable: pd.DataFrame,
                     recs: list, daily: pd.DataFrame, insights: list[Insight]) -> bytes:
    """產生整月分析報告 PDF，回傳檔案內容（bytes）。"""
    buf = io.BytesIO()
    plan_col = f"{plan_name}(元)"
    meta = {"Title": f"HEMS 整月分析報告 {year}-{month:02d}", "Author": "HEMS"}
    # pdf.fonttype 42：嵌入 TrueType 字型，中文可選取、搜尋
    with matplotlib.rc_context({"pdf.fonttype": 42}), PdfPages(buf, metadata=meta) as pdf:
        page = _TextPages(pdf, f"HEMS 整月分析報告：{year} 年 {month} 月")
        page.line(f"{house_name}｜電價方案：{plan_name}｜產生日期：{date.today()}", 9.5, "#52514e", gap=0.01)
        page.line("各模式比較", 12, bold=True, gap=0.004)
        summary = _summary_table(mtable, plan_col)
        page.table(summary, 0.03 * (len(summary) + 1))
        if insights:
            page.line("數據分析", 12, bold=True, gap=0.004)
            for i in insights:
                page.line(f"● {i.title}", 10.5, bold=True)
                page.line(_plain(i.detail), 9.5, "#52514e", gap=0.006)
        page.close()

        _charts_page(pdf, daily, f"{year} 年 {month} 月：每日用電與冷氣")

        text = _TextPages(pdf, "節省建議")
        for n, r in enumerate(recs, start=1):
            text.line(f"{n}. {r.headline()}", 10.5, bold=True)
            text.line(r.detail, 9.5, "#52514e")
            if r.note:
                text.line(f"※ {r.note}", 9, "#898781")
            text.y -= 0.008
        text.line("說明：以中央氣象署臺中測站逐時資料推演；每個模式每天各自最佳化。"
                  "流動電費依逐時電價計算，月電費含基本電費與累進級距。", 8.5, "#898781")
        text.close()
    return buf.getvalue()
