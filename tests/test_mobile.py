"""C3：手機版排版（CSS 斷點；實際畫面需在手機或瀏覽器的裝置模式檢查）。"""
import re

from ui.floorplan import FRONTEND_DIR
from ui.theme import GLOBAL_CSS


def _balanced(css: str) -> bool:
    depth = 0
    for ch in css:
        depth += {"{": 1, "}": -1}.get(ch, 0)
        if depth < 0:
            return False
    return depth == 0


def test_global_css_has_mobile_breakpoints():
    assert "@media (max-width: 640px)" in GLOBAL_CSS
    assert "@media (max-width: 380px)" in GLOBAL_CSS
    mobile = GLOBAL_CSS.split("@media (max-width: 640px)")[1]
    for selector in (".hems-kpi .value", ".hems-chips", ".hems-cmp", ".block-container"):
        assert selector in mobile
    assert _balanced(GLOBAL_CSS.removeprefix("<style>").removesuffix("</style>"))


def test_floorplan_mobile_css():
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    style = re.search(r"<style>(.*)</style>", html, re.S).group(1)
    assert "@media (max-width: 520px)" in style and "@media (max-width: 760px)" in style
    assert _balanced(style)
