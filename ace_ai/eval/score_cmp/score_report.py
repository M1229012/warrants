import os, pandas as pd, numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
r = pd.read_pickle(os.path.join(HERE, "score_compare.pkl"))
print(f"樣本 {len(r)}（{r.code.nunique()} 檔 × {r.date.nunique()} 日）")


def dist(col):
    s = r[col]
    return (f"{col:5s} 平均 {s.mean():5.1f}｜中位 {s.median():5.1f}｜P90 {s.quantile(.9):5.1f}｜"
            f"≥75 {100*(s>=75).mean():4.1f}%｜≥60 {100*(s>=60).mean():4.1f}%")


for c in ("OLD", "A", "B"):
    print(dist(c))

for a, b in (("OLD", "A"), ("A", "B")):
    d = r[b] - r[a]
    print(f"\n{a}→{b}：變動 {100*(d!=0).mean():.1f}%（加分 {100*(d>0).mean():.1f}%、減分 {100*(d<0).mean():.1f}%）"
          f"｜平均 {d.mean():+.2f}｜加分者平均 {d[d>0].mean() if (d>0).any() else 0:+.2f}｜最大 {d.max():+.1f}／{d.min():+.1f}")
    ranks = []
    for day, g in r.groupby("date"):
        ra, rb = g[a].rank(ascending=False), g[b].rank(ascending=False)
        top_a, top_b = set(g.nlargest(30, a).code), set(g.nlargest(30, b).code)
        ranks.append((ra.corr(rb, method="spearman"), len(top_a & top_b), (ra - rb).abs().median()))
    ranks = np.array(ranks)
    print(f"  排名 Spearman {ranks[:,0].mean():.3f}｜前 30 名重疊 {ranks[:,1].mean():.1f}/30｜名次變動中位 {ranks[:,2].mean():.0f}")

# Separate each rule effect; combined changes can include interactions.
d = r.A != r.OLD
individual = {c: r[c] != r.OLD for c in ("NEAR", "MERGE", "DIST", "PATH")}
for c, changed in individual.items():
    print(f"分項 {c}：變動 {changed.sum()} 筆，平均 {(r[c]-r.OLD).mean():+.2f}")
expected_delta = sum(r[c]-r.OLD for c in individual)
interaction = (r.A-r.OLD)-expected_delta
print(f"交互作用：{(interaction.abs()>0.01).sum()} 筆；與各分項加總不同時，需核對明細")
# Do not claim combined-score attribution from support rules alone.
weak = r[(r.OLD < 60) & (r.A > r.OLD)].copy()
weak["entered75"] = (weak.A >= 75)
weak["entered30"] = False
for day,g in r.groupby("date"):
    newtop=set(g.nlargest(30,"A").code)-set(g.nlargest(30,"OLD").code)
    weak.loc[(weak.date==day)&weak.code.isin(newtop),"entered30"]=True
print(f"原低於60分的加分股：{len(weak)}；進75分 {weak.entered75.sum()}；新進前30名 {weak.entered30.sum()}")
weak.to_csv(os.path.join(HERE,"weak_gainers.csv"),index=False,encoding="utf-8-sig")
bd = r.B != r.A
print(f"歸因 A→B 變動 {bd.sum()} 筆，其中 MA5 跌停也續揚 {(bd & r.ma5_limit).sum()}；MA5 條件成立比率 {100*r.ma5_limit.mean():.1f}%")

x = r[r.code == "3406"].sort_values("date")
if len(x):
    print("\n3406 玉晶光：")
    for _, row in x.iterrows():
        print(f"  {row.date} OLD {row.OLD} → A {row.A} → B {row.B}")
    last = x.iloc[-1]
    print("  OLD 支撐：", last.OLD_sup)
    print("  A   支撐：", last.A_sup)
    print("  MA20 文字：", last.ma20_text)

print("\nA 加分最多 5 例：")
r["dA"] = r.A - r.OLD
for _, row in r.nlargest(5, "dA").iterrows():
    print(f"  {row.code} {row.date} {row.OLD}→{row.A}｜{row.A_sup}")
print("A 減分最多 3 例：")
for _, row in r.nsmallest(3, "dA").iterrows():
    print(f"  {row.code} {row.date} {row.OLD}→{row.A}｜舊：{row.OLD_sup}｜新：{row.A_sup}")
seq = r[r.ma20_text.str.contains("→")]
print(f"\nMA20 文字出現 3 日序列 {len(seq)} 筆，例：", seq.ma20_text.head(3).tolist())
