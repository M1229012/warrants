"""三角自我檢查：每張 3x3 縮圖（近 100 根 K、上緣橘、下緣藍、標題寫判定），輸出 grid_*.png。"""
import sys, importlib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
sys.argv = [sys.argv[0], "141"] + sys.argv[1:]
src = open("tri_dev.py", encoding="utf-8").read().split("bars = int")[0]
exec(src)
plt.rcParams["font.family"] = "Microsoft JhengHei"
codes = sys.argv[2].split(",")
for page in range(0, len(codes), 9):
    fig, axes = plt.subplots(3, 3, figsize=(15, 10))
    for ax, code in zip(axes.flat, codes[page:page + 9]):
        df = load(code).iloc[-141:]; end = len(df) - 1
        H, L, O, C = (df[k].to_numpy(float) for k in ("High", "Low", "Open", "Close"))
        t = kp.user_triangle(df, kp._atr_prev(H, L, C), end)
        show = range(end - 99, end + 1)
        for i in show:
            col = "#d9534f" if C[i] >= O[i] else "#2a9d8f"
            ax.vlines(i, L[i], H[i], color=col, lw=0.6)
            ax.vlines(i, min(O[i], C[i]), max(O[i], C[i]), color=col, lw=2.5)
        title = f"{code} 無三角"
        if t:
            for edge, colr in (("upper", "#e67e22"), ("lower", "#1f77b4")):
                s, k = t[edge]; a0 = max(t["anchors"][edge][0], end - 99)
                ax.plot([a0, end + 10], [s * a0 + k, s * (end + 10) + k], color=colr, lw=1.5)
            title = f"{code} {t['kind']}｜{t['state']}"
        ax.set_title(title, fontsize=10)
        ax.set_xticks([end - 99, end - 66, end - 33, end])
        ax.set_xticklabels([df.index[i].strftime("%m-%d") for i in (end - 99, end - 66, end - 33, end)], fontsize=7)
        ax.tick_params(axis="y", labelsize=7)
    fig.tight_layout()
    fig.savefig(f"grid_{page // 9}.png", dpi=70)
    plt.close(fig)
print("ok")
