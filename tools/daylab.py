"""デイトレードのルールを、最新のデータで検証し直す（毎日15:30の更新で動かし、Claude がルールの見直しに使う）。

  python3 tools/daylab.py      … 検証結果を表示し、docs/data/daylab.json（ページの「デイトレードの検証」）に書く

検証に使うデータ:
  - 日足 3年分（寄り付きで買って大引けで売る形。M1 など）
  - 5分足 直近60日（前場・後場の途中で売買する形。P1 など。Yahoo で取れるのが60日まで）
  - 実際の仮想売買の成績（data/trade/portfolio.json のルール名付きの記録）
どの形も、手数料・滑りとして1回0.05%を引いた後の値。1日に複数銘柄を買う形は、その日の平均を1日の成績とする。
"""
import json
import os
import sys
import warnings
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auto as A  # noqa: E402
import trade as T  # noqa: E402

OUT = os.path.join(T.ROOT, "docs", "data", "daylab.json")
COST = 0.0005


def stats(dm):
    """日ごとの成績（Series, index=日付）→ 平均・勝ち日・t値・年別。"""
    dm = dm.dropna()
    if len(dm) < 5:
        return dict(days=int(len(dm)))
    t = dm.mean() / (dm.std() / np.sqrt(len(dm))) if dm.std() > 0 else 0.0
    yrs = {str(y): round(float(s.mean()) * 100, 3) for y, s in dm.groupby(dm.index.year)}
    return dict(days=int(len(dm)), mean=round(float(dm.mean()) * 100, 3), win=round(float((dm > 0).mean()) * 100, 1),
                t=round(float(t), 2), worst=round(float(dm.min()) * 100, 2), years=yrs)


def daily_frame():
    codes = list(T.DAY_LIST) + ["1306"]
    X = T._yf(codes, period="3y", interval="1d")
    rows = []
    for c, x in X.items():
        x = x[(x.Open > 0) & (x.Volume > 0)].copy()
        x.index = pd.DatetimeIndex(x.index).tz_localize(None).normalize()
        f = pd.DataFrame(dict(o=x.Open, c=x.Close))
        f["pc"] = f.c.shift(1)
        f["pr1"] = (f.c / f.pc - 1).shift(1)
        f["gap"] = f.o / f.pc - 1
        f["oc"] = f.c / f.o - 1
        f["code"] = c
        rows.append(f[(f.gap.abs() < 0.25)])
    r = pd.concat(rows).dropna(subset=["gap", "oc", "pr1"])
    r.index.name = "day"
    r = r.reset_index()
    r["mgap"] = r.day.map(r[r.code == "1306"].set_index("day").gap)   # 市場全体（1306）の窓
    return r[r.code != "1306"]


def intraday_frame():
    codes = list(T.DAY_LIST) + ["1306"]
    X = T._yf(codes, period="60d", interval="5m")
    D = T._yf(codes, period="6mo", interval="1d")
    rows = []
    for c, x in X.items():
        d = D.get(c)
        if d is None:
            continue
        d = d.copy()
        d.index = pd.DatetimeIndex(d.index).tz_localize(None).normalize()
        x = x.copy()
        x.index = pd.DatetimeIndex(x.index).tz_convert(T.JST)
        for day, g in x.groupby(x.index.date):
            day = pd.Timestamp(day)
            if day not in d.index:
                continue
            h = g.index.strftime("%H:%M")
            if h[0] > "09:05":
                continue
            mor = g[h < "11:30"]
            if not len(mor):
                continue

            def op(k):
                s = g[h >= k]
                return float(s.Open.iloc[0]) if len(s) else np.nan
            i = d.index.get_loc(day)
            pc = float(d.Close.iloc[i - 1]) if i > 0 else np.nan
            o, mc, hi, lo = float(g.Open.iloc[0]), float(mor.Close.iloc[-1]), float(mor.High.max()), float(mor.Low.min())
            f50 = g[h < "09:50"]
            rows.append(dict(code=c, day=day, pc=pc, o=o, mc=mc, dc=float(d.Close[day]), mret=mc / o - 1,
                             mpos=(mc - lo) / (hi - lo) if hi > lo else 0.5, r50=float(f50.Close.iloc[-1]) / o - 1 if len(f50) else np.nan,
                             e1010=op("10:10"), e1240=op("12:40")))
    r = pd.DataFrame(rows)
    mk = r[r.code == "1306"].set_index("day")
    r = r[r.code != "1306"].copy()
    r["mk_m"] = r.day.map(mk.mret)
    r["mk_50"] = r.day.map(mk.r50)
    r["rel_m"] = r.mret - r.mk_m
    r["mgap"] = r.day.map(mk.o / mk.pc - 1)
    r["gap"] = r.o / r.pc - 1
    r["pr1"] = np.nan
    return r


def m1_days(r, R, exit_col="oc"):
    g = r[(r.gap >= R["gap_lo"]) & (r.gap <= R["gap_hi"])]
    if R.get("prev_down"):
        g = g[g.pr1 <= 0]
    if R.get("mkt_gap") is not None:          # 市場全体が大きく下げて始まる日は休む
        g = g[g.mgap > R["mkt_gap"]]
    g = g.sort_values("gap").groupby("day").head(R["max"])
    return g.groupby("day")[exit_col].mean() - COST


def p1_days(r, R):
    g = r[(r.mk_m >= R["mkt"]) & (r.rel_m >= R["rel_lo"]) & (r.rel_m <= R["rel_hi"]) & (r.mpos >= R["pos"])]
    g = g.sort_values("rel_m", ascending=False).groupby("day").head(R["max"])
    return (g.dc / g.e1240 - 1).groupby(g.day).mean() - COST


def live(pf, rule):
    s = [x for x in pf["trades"] if x["kind"] == "sell" and x.get("rule") == rule]
    if not s:
        return dict(n=0)
    p = np.array([x["pnl_pct"] for x in s])
    return dict(n=len(s), win=round(float((p > 0).mean()) * 100, 1), mean=round(float(p.mean()), 3),
                yen=int(sum(x["pnl"] for x in s)))


def fmt(st):
    if "mean" not in st:
        return f"日数{st.get('days', 0)}（不足）"
    yrs = " ".join(f"{k}:{v:+.2f}" for k, v in st["years"].items())
    return f"{st['days']}日 平均{st['mean']:+.3f}% 勝ち日{st['win']:.0f}% t={st['t']:+.2f} 最悪日{st['worst']:+.1f}% 年別 {yrs}"


def main():
    R = A.rules()
    pf = T.load()
    print("データ取得中（日足3年・5分足60日）…")
    dr = daily_frame()
    ir = intraday_frame()
    last60 = sorted(ir.day.unique())
    cut60 = pd.Timestamp(last60[0]) if last60 else None
    res = []

    m1 = m1_days(dr, R["M1"])
    m1r = m1[m1.index >= cut60] if cut60 is not None else m1.iloc[:0]
    # 前引けで売る場合（5分足のある60日だけ）
    ir2 = ir.copy()
    ir2["pr1"] = ir2.groupby("code").apply(lambda g: (g.dc / g.pc - 1).shift(1)).reset_index(level=0, drop=True)
    ir2["oc_am"] = ir2.mc / ir2.o - 1
    m1am = m1_days(ir2.dropna(subset=["pr1"]), R["M1"], "oc_am")
    res.append(dict(id="M1", name=R["M1"]["name"], on=R["M1"]["on"],
                    desc=f"前日も下げ、{R['M1']['gap_lo'] * 100:.1f}〜{R['M1']['gap_hi'] * 100:.1f}%下げて始まった銘柄を、窓の大きい順に{R['M1']['max']}銘柄まで寄り付きで買い、大引けで売る"
                         + (f"。市場全体（1306）が{R['M1']['mkt_gap'] * 100:.0f}%以下で始まる日は休む（寄り付き前は日経平均先物の{R['M1']['fut_gap'] * 100:.1f}%以下で判定）"
                            if R["M1"].get("mkt_gap") is not None else ""),
                    bt=stats(m1), recent=stats(m1r), live=live(pf, "M1"),
                    note=f"同じ条件で前引けに売った場合（直近60日）: {fmt(stats(m1am))}"))
    p1 = p1_days(ir, R["P1"])
    res.append(dict(id="P1", name=R["P1"]["name"], on=R["P1"]["on"],
                    desc=f"市場の前場が+{R['P1']['mkt'] * 100:.1f}%以上の日に、前場で市場より{R['P1']['rel_lo'] * 100:+.0f}〜{R['P1']['rel_hi'] * 100:+.0f}%強く高値圏で引けた銘柄を12:40ごろ買い、大引けで売る",
                    bt=stats(p1), recent=stats(p1), live=live(pf, "P1"), note="5分足が60日分しかないため、検証の日数が少ない"))

    # 候補（採用していない形。毎日成績を見て、条件を満たせば採用を検討する）
    cands = []
    if R["M1"].get("mkt_gap") is not None:
        cands.append(dict(name="（比較）M1で市場全体の窓の条件を外した場合", bt=stats(m1_days(dr, dict(R["M1"], mkt_gap=None)))))
    g = dr[(dr.gap <= -0.015) & (dr.gap >= -0.06)]
    cands.append(dict(name="窓下げ買い（前日の上げ下げは問わない）", bt=stats(g.groupby("day").oc.mean() - COST)))
    g = dr[(dr.gap >= 0.015)]
    cands.append(dict(name="窓上げ買い（+1.5%以上で始まった銘柄）", bt=stats(g.groupby("day").oc.mean() - COST)))
    g = ir[(ir.mk_50 <= -0.003) & ((ir.e1010 > 0))]
    g = g[(g.r50 - g.mk_50) <= -0.01]
    cands.append(dict(name="10時の押し目買い（市場も個別も9:50まで弱い）→大引け", bt=stats((g.dc / g.e1010 - 1).groupby(g.day).mean() - COST)))
    g = ir[(ir.r50 - ir.mk_50) >= 0.01]
    cands.append(dict(name="10時の強い銘柄買い（9:50まで市場より+1%強い）→前引け", bt=stats((g.mc / g.e1010 - 1).groupby(g.day).mean() - COST)))
    cands.append(dict(name="（比較）全銘柄を寄り付きで買い大引けで売る", bt=stats(dr.groupby("day").oc.mean() - COST)))

    print(f"\nデイトレードの検証 {T.now():%Y-%m-%d %H:%M}（手数料・滑り0.05%を引いた後、1日あたり）")
    for x in res:
        print(f"[{x['id']} {'採用中' if x['on'] else '停止中'}] {x['name']}：{x['desc']}")
        print(f"   検証   {fmt(x['bt'])}")
        if x["id"] == "M1":
            print(f"   直近60日 {fmt(x['recent'])}")
        lv = x["live"]
        print(f"   実際の仮想売買 {lv['n']}回" + (f" 勝率{lv['win']:.0f}% 平均{lv['mean']:+.2f}% 損益{lv['yen']:+,}円" if lv["n"] else ""))
        print(f"   {x['note']}")
    print("候補（採用していない形）:")
    for x in cands:
        print(f"   {x['name']}：{fmt(x['bt'])}")
    print("見直しの目安：採用は「平均+0.10%以上・t値2以上・マイナスの年がない」。採用中でも、直近60日が−0.30%を下回るか、"
          "実際の売買が20回以上で平均がマイナスなら、停止や条件の変更を検討する。変えたら day_rules.json の log に理由を書く。")
    out = dict(updated=T.now().strftime("%Y-%m-%d %H:%M"), rules=res, candidates=cands, log=R.get("log", [])[-20:],
               data=dict(daily_from=str(dr.day.min().date()), daily_to=str(dr.day.max().date()),
                         intraday_from=str(ir.day.min().date()) if len(ir) else None, intraday_to=str(ir.day.max().date()) if len(ir) else None,
                         stocks=int(dr.code.nunique())))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n書き出し: docs/data/daylab.json（日足 {out['data']['daily_from']}〜{out['data']['daily_to']}・5分足 {out['data']['intraday_from']}〜{out['data']['intraday_to']}・{out['data']['stocks']}銘柄）")


if __name__ == "__main__":
    main()
