"""產生程式圖示 assets/hems.ico：藍色圓角底、白色房屋、黃色閃電（顏色同圖表）。

用法：python scripts/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
BLUE, WHITE, YELLOW = "#2a78d6", "#ffffff", "#eda100"
SIZE = 512   # 先畫大圖再縮小，邊緣比較平滑


def draw() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([16, 16, SIZE - 16, SIZE - 16], radius=110, fill=BLUE)
    # 房屋：屋頂三角形 + 屋身
    d.polygon([(256, 92), (436, 250), (76, 250)], fill=WHITE)
    d.rectangle([124, 240, 388, 420], fill=WHITE)
    # 閃電
    d.polygon([(278, 228), (190, 342), (250, 342), (226, 430), (326, 302), (264, 302), (296, 228)], fill=YELLOW)
    return img


def main() -> None:
    out = ROOT / "assets"
    out.mkdir(exist_ok=True)
    img = draw()
    img.save(out / "hems.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.resize((256, 256), Image.LANCZOS).save(out / "hems.png")
    print(f"已產生 {out / 'hems.ico'}")


if __name__ == "__main__":
    main()
