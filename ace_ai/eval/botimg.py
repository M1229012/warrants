import sys, os
sys.argv = [sys.argv[0], "142"] + sys.argv[1:]
exec(open("tri_dev.py", encoding="utf-8").read().split("bars = int")[0])
import kline_debug as kd
from PIL import Image
imgs = []
for code in sys.argv[2].split(","):
    df = load(code).iloc[-142:]
    try:
        data = kd.build_panel(df, None, code, code)["kline_debug"]
        img = kd.render(data).convert("RGB")
        img = img.crop((0, 0, img.width, min(img.height, 1000))).resize((750, 500))
    except Exception as exc:
        img = Image.new("RGB", (750, 500), "white"); print(code, exc)
    imgs.append(img)
cols = 2; rows = (len(imgs) + 1) // 2
sheet = Image.new("RGB", (750 * cols, 500 * rows), "white")
for i, im in enumerate(imgs):
    sheet.paste(im, ((i % cols) * 750, (i // cols) * 500))
sheet.save(sys.argv[3] if len(sys.argv) > 3 else "bot_sheet.png")
print("ok")
