"""テクニカル指標・選定・売買ライン・決算予想・根拠文の生成。"""
import math

import numpy as np
import pandas as pd

# 過去検証で得た値（README・ページの「検証」と同じ数字）
EV_ATR = {"main": 0.348, "rebound": 0.894}   # 損切り・利確ルール込みの1回あたり期待値（ATR倍）
EARN_ABS_DEFAULT = 6.0                        # 決算2日間の平均値幅（対日経・絶対値, %）


# ---------------------------------------------------------------- テクニカル
def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def atr(x, n=14):
    pc = x.Close.shift()
    tr = pd.concat([x.High - x.Low, (x.High - pc).abs(), (x.Low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def technicals(x, n225, d_from, d_to, today_partial):
    """d_from, d_to: 信用残の比較期間（Timestamp）。today_partial: 最終行が場中の途中値か。"""
    if len(x) < 210:
        return None
    c = x.Close
    full = x.iloc[:-1] if today_partial else x
    r = dict(price=float(c.iloc[-1]), prev=float(c.iloc[-2]))
    for n in (5, 25, 75, 200):
        r[f"ma{n}"] = float(c.rolling(n).mean().iloc[-1])
    r["ma75_prev10"] = float(c.rolling(75).mean().iloc[-11])
    r["rsi"] = float(rsi(c).iloc[-1])
    a = float(atr(x).iloc[-1])
    r["atr"], r["atrp"] = a, a / r["price"] * 100
    r["lo20"], r["hi20"] = float(x.Low.iloc[-20:].min()), float(x.High.iloc[-20:].max())
    r["hi60"], r["hi250"] = float(x.High.iloc[-60:].max()), float(x.High.iloc[-250:].max())
    r["ret21"] = (c.iloc[-1] / c.iloc[-22] - 1) * 100
    p0, p1 = c.asof(d_from), c.asof(d_to)
    r["pchg"] = (p1 / p0 - 1) * 100 if p0 and p1 else np.nan
    n_now, n_10 = n225.Close.iloc[-1], n225.Close.iloc[-11]
    r["rs"] = ((c.iloc[-1] / c.iloc[-11]) - (n_now / n_10)) * 100
    i20 = -21
    r["pre20"] = ((c.iloc[-1] / c.iloc[i20]) - (n_now / n225.Close.asof(c.index[i20]))) * 100
    r["vol20"] = float(full.Volume.iloc[-20:].mean())
    r["val20"] = float((full.Volume * full.Close).iloc[-20:].mean() / 1e8)
    r["val5"] = float((full.Volume * full.Close).iloc[-5:].mean() / 1e8)      # 直近5日（注目度の変化を見る）
    r["up"] = bool(r["price"] > r["ma75"] and r["ma25"] > r["ma75"])
    r["above200"] = bool(r["price"] > r["ma200"])
    r["dev25"] = (r["price"] / r["ma25"] - 1) * 100
    return r


def _tick(p):
    return 1 if p <= 3000 else 5 if p <= 30000 else 10


def _rnd(p, mode):
    tk = _tick(p)
    f = {"near": round, "down": math.floor}[mode]
    return int(f(p / tk) * tk)


def levels(r):
    c, a = r["price"], r["atr"]
    if c > r["ma25"]:
        e = min(c - 0.3 * a, max(r["ma25"], c - 1.0 * a))
    else:
        e = min(c - 0.3 * a, max(r["lo20"] + 0.2 * a, c - 1.0 * a))
    sl, tp1, tp2, cap = e - 1.5 * a, e + 1.5 * a, e + 3.0 * a, ""
    for lv, nm in ((r["hi60"], "60日高値"), (r["hi250"], "52週高値")):
        if tp1 + 0.5 * a < lv < tp2:
            tp2, cap = lv - 0.1 * a, nm
            break
    e, sl, tp1, tp2 = _rnd(e, "down"), _rnd(sl, "down"), _rnd(tp1, "near"), _rnd(tp2, "near")
    return dict(entry=e, stop=sl, tp1=tp1, tp2=tp2, tp2cap=cap,
                risk=(e - sl) / e * 100, tp1p=(tp1 - e) / e * 100, tp2p=(tp2 - e) / e * 100,
                avgp=((tp1 + tp2) / 2 - e) / e * 100, dip=(c - e) / c * 100)


# ---------------------------------------------------------------- 決算予想
def _fy_quarters(fy, fm):
    """会計年度(fy年fm月決算)に属する四半期末 (年, 月) を古い順に。"""
    out = []
    for k in (9, 6, 3, 0):
        m = fm - k
        y = fy
        while m <= 0:
            m += 12
            y -= 1
        out.append((y, m))
    return out


def earnings_view(fin, cons, x, n225, earn_date, bd_earn, pre20, price):
    """決算の上振れ確率と想定値幅。fin=株探の業績, cons=株予報。取れない項目は使わない。"""
    v = dict(earn=earn_date or "", bdEarn=bd_earn, factors=[])
    gap = prog = prog_ly = prev_yoy = cons_gap = target_up = None
    if fin:
        ann = fin["annual"]
        fc = [a for a in ann if a["fc"] and a.get("経常益")]
        act = {(a["fy"], a["fm"]): a for a in ann if not a["fc"]}
        qd = {(q["y"], q["m"]): q for q in fin["quarters"]}
        if fc:
            f = fc[-1]
            ly = act.get((f["fy"] - 1, f["fm"]))
            qs = _fy_quarters(f["fy"], f["fm"])
            done = [k for k in qs if k in qd and qd[k].get("経常益") is not None]
            if done and ly and ly.get("経常益") and ly["経常益"] > 0 and f["経常益"] > 0:
                lyq = [(k[0] - 1, k[1]) for k in done]
                if all(k in qd and qd[k].get("経常益") is not None for k in lyq):
                    prog = sum(qd[k]["経常益"] for k in done) / f["経常益"] * 100
                    prog_ly = sum(qd[k]["経常益"] for k in lyq) / ly["経常益"] * 100
                    gap = prog - prog_ly
                    v["nq"] = len(done)
            v["fc_ord"] = f["経常益"]
            if ly and ly.get("経常益"):
                v["fc_growth"] = (f["経常益"] / ly["経常益"] - 1) * 100 if ly["経常益"] > 0 else None
        qs_sorted = sorted(qd)
        if qs_sorted:
            last = qs_sorted[-1]
            prevy = (last[0] - 1, last[1])
            if prevy in qd and qd[prevy].get("経常益") and qd[prevy]["経常益"] > 0 and qd[last].get("経常益") is not None:
                prev_yoy = (qd[last]["経常益"] / qd[prevy]["経常益"] - 1) * 100
    if cons and cons.get("ord_cons") and cons.get("ord_co") and cons["ord_co"] > 0:
        cons_gap = (cons["ord_cons"] / cons["ord_co"] - 1) * 100
    if cons and cons.get("target"):
        target_up = (cons["target"] / price - 1) * 100
    # 過去の決算反応（発表前日終値→翌営業日終値、対日経）
    reacts = []
    if fin and x is not None:
        C, idx = x.Close, x.Close.index
        for q in fin["quarters"]:
            D = q.get("date")
            if D is None:
                continue
            i = idx.searchsorted(pd.Timestamp(D))
            if i < 1 or i + 1 >= len(idx):
                continue
            r = (C.iloc[i + 1] / C.iloc[i - 1] - 1) - (n225.Close.asof(idx[i + 1]) / n225.Close.asof(idx[i - 1]) - 1)
            reacts.append(r * 100)
    M = float(np.mean(np.abs(reacts))) if len(reacts) >= 3 else EARN_ABS_DEFAULT
    # 合成
    F = 0.0
    if gap is not None:
        F += 0.5 * max(-1, min(1, gap / 10))
    if prev_yoy is not None:
        F += 0.4 if prev_yoy >= 50 else 0.15 if prev_yoy >= 20 else -0.1 if prev_yoy <= -20 else 0
    E = 0.0
    if cons_gap is not None:
        if cons_gap >= 10:
            E = 0.2 if F >= 0.3 else -0.3
        elif cons_gap >= 3:
            E = 0.1 if F >= 0.2 else -0.1
        elif cons_gap <= -5:
            E = 0.1 if F >= 0.2 else -0.2
    Pz = -0.35 if pre20 >= 10 else -0.1 if pre20 >= 5 else 0.05 if pre20 <= -10 else 0
    z = F + E + Pz
    P = max(0.25, min(0.75, 0.48 + 0.15 * z))
    Er = (2 * P - 1) * M
    v.update(P=round(P * 100), Er=round(Er, 1), M=round(M, 1), z=round(z, 2),
             gap=None if gap is None else round(gap, 1), prog=None if prog is None else round(prog, 1),
             prog_ly=None if prog_ly is None else round(prog_ly, 1),
             prev_yoy=None if prev_yoy is None else round(prev_yoy, 1),
             cons_gap=None if cons_gap is None else round(cons_gap, 1),
             target_up=None if target_up is None else round(target_up, 1),
             pre20=round(pre20, 1), n_react=len(reacts),
             up_react=sum(1 for r in reacts if r > 0))
    fs = v["factors"]
    if gap is not None:
        fs.append(dict(k="進捗率", t=f"経常益の進捗率 {prog:.0f}%（前年同期 {prog_ly:.0f}%）",
                       s=1 if gap >= 3 else -1 if gap <= -3 else 0))
    if prev_yoy is not None:
        fs.append(dict(k="直近四半期", t=f"直近四半期の経常益 前年比 {prev_yoy:+.0f}%",
                       s=1 if prev_yoy >= 20 else -1 if prev_yoy <= -20 else 0))
    if cons_gap is not None:
        fs.append(dict(k="市場予想", t=f"市場予想（コンセンサス）は会社予想より {cons_gap:+.1f}%",
                       s=(1 if F >= 0.2 else -1) if cons_gap >= 3 else (-1 if F < 0.2 else 0) if cons_gap <= -5 else 0))
    fs.append(dict(k="決算前の上昇", t=f"直近20日の対日経 {pre20:+.1f}%",
                   s=-1 if pre20 >= 10 else 0 if pre20 > -10 else 1))
    if len(reacts) >= 3:
        fs.append(dict(k="過去の反応", t=f"過去{len(reacts)}回の決算で上昇{v['up_react']}回・平均値幅 ±{M:.1f}%", s=0))
    if target_up is not None:
        fs.append(dict(k="目標株価", t=f"アナリスト目標株価まで {target_up:+.1f}%", s=1 if target_up >= 10 else -1 if target_up <= -5 else 0))
    v["label"] = "上振れ期待" if P >= 0.58 else "下振れ警戒" if P <= 0.42 else "中立"
    if bd_earn is None:
        v["action"], v["hold"] = "決算予定日が未発表。発表されたら再判定", 0.0
    elif bd_earn > 20:
        v["action"], v["hold"] = "保有期間（20営業日）の後なので影響なし", 0.0
    elif P >= 0.60:
        v["action"], v["hold"] = "決算をまたいでよい（利確1に届いていれば残り半分で）", 1.0
    elif P <= 0.42:
        v["action"], v["hold"] = ("決算後まで新規の買いは見送り" if bd_earn <= 5 else "決算日の前日までに全部売る"), 0.0
    else:
        v["action"], v["hold"] = "決算日の前日までに半分以上を売る", 0.5
    if pre20 >= 10 and bd_earn is not None and bd_earn <= 20:
        v["after"] = "決算前に大きく上げた銘柄は、決算翌日に売られても、その後20日は上がりやすい傾向（過去平均 +4.0%）。決算後の下げは買い直しの候補。"
    return v


def p_open(bd):
    if bd is None or bd > 20:
        return 0.0
    return 0.8 if bd <= 5 else 0.6 if bd <= 10 else 0.4


# ---------------------------------------------------------------- 選定とスコア
def classify(r):
    base = (r["val20"] >= 3 and r["rate"] <= -10 and r["dc0"] >= 1.0 and
            (r["rate4"] is None or r["rate4"] <= -8))
    if not base:
        return ""
    if -2 <= r["pchg"] <= 15 and r["rsi"] <= 68 and r["dev25"] <= 8 and r["atrp"] <= 4.5:
        return "main"
    if r["pchg"] <= -3:
        return "rebound"
    return ""


def score(r):
    s = min(20, -r["rate"] * 0.6) + min(10, max(0, r["dc0"]) * 2.5)
    s += (5 if r["price"] > r["ma75"] else 0) + (5 if r["ma25"] > r["ma75"] else 0) + (5 if r["above200"] else 0)
    s += 10 if 45 <= r["rsi"] <= 62 else 6 if (62 < r["rsi"] <= 68 or 38 <= r["rsi"] < 45) else 0
    s += 10 if r["atrp"] < 2 else 7 if r["atrp"] < 3 else 4 if r["atrp"] < 4 else 1
    rt = r["ratio1"] if r["ratio1"] is not None else 99
    s += 6 if rt < 3 else 4 if rt < 8 else 2 if rt < 15 else 0
    if r.get("sell_chg") is not None and r["sell_chg"] > 10 and rt < 8:
        s += 2
    ev = r.get("earnv") or {}
    if ev.get("bdEarn") is not None and ev["bdEarn"] <= 20:
        s += max(-8, min(8, (ev["P"] - 50) * 0.4))
        if ev["bdEarn"] <= 5 and ev["P"] < 58:
            s -= 6
    return s


NEG_WORDS = ("下方修正", "減益", "赤字", "下振れ", "減配", "無配", "特別損失", "不適切", "監理", "上場廃止")
POS_WORDS = ("上方修正", "増益", "最高益", "上振れ", "増配", "自社株買い", "自己株式の取得", "株式分割", "大量保有")


def news_flags(news):
    pos, neg = [], []
    for n in news or []:
        h = n.get("h", "")
        if any(w in h for w in NEG_WORDS) and "について" not in h[:12]:
            neg.append(n)
        elif any(w in h for w in POS_WORDS):
            pos.append(n)
    return pos[:2], neg[:2]


def grade(r):
    ev = r.get("earnv") or {}
    neg = r.get("neg_news")
    if neg:
        return "監視"
    if ev.get("bdEarn") is not None and ev["bdEarn"] <= 10 and ev.get("P", 50) <= 42:
        return "監視"
    if r["group"] == "main":
        if r["score"] >= 46 and (r["up"] or r["above200"]):
            return "本命"
        return "次点" if r["score"] >= 36 else "監視"
    if r["score"] >= 44 and r["up"] and r["atrp"] < 5:
        return "本命"
    return "次点" if r["score"] >= 34 and r["atrp"] < 7 else "監視"


def fmtn(v, d=0):
    return f"{v:,.{d}f}"


def reasons(r, n225chg):
    pros, cons = [], []
    rate4 = f"、4週間では{r['rate4']:+.0f}%" if r["rate4"] is not None else ""
    pros.append(f"減る前の買い残は出来高の{r['dc0']:.1f}日分あった。それが{r['rate_days']}営業日で{r['rate']:+.0f}%減った{rate4}。")
    if r["group"] == "rebound":
        pros[-1] += f"同じ期間に株価は{r['pchg']:+.1f}%下げており、投げ売りで整理が進んだ形。"
    if r["up"] and r["above200"]:
        pros.append("25日線＞75日線で200日線の上。上昇トレンドが続いている。")
    elif r["above200"]:
        pros.append("株価は200日線の上にある。")
    if r["rs"] >= 5:
        pros.append(f"直近10営業日は日経平均より{r['rs']:+.0f}%強い。")
    if r["atrp"] < 2.2:
        pros.append(f"1日の値幅（ATR）が{r['atrp']:.1f}%と小さく、損切り幅も{r['lv']['risk']:.1f}%で済む。")
    rt = r["ratio1"]
    if rt is not None and rt < 3:
        pros.append(f"信用倍率{rt:.1f}倍で売りと買いが拮抗。売り方の買い戻しが上げを助ける。")
    if r.get("sell_chg") is not None and r["sell_chg"] > 15 and (rt or 99) < 15:
        pros.append(f"売り残は{r['sell_chg']:+.0f}%増え、将来の買い戻し需要になる。")
    ev = r.get("earnv") or {}
    if ev.get("gap") is not None and ev["gap"] >= 3:
        pros.append(f"経常益の進捗率は{ev['prog']:.0f}%で、前年同期の{ev['prog_ly']:.0f}%を上回る。上方修正の余地がある。")
    if ev.get("cons_gap") is not None and ev["cons_gap"] >= 3 and ev.get("gap") is not None and ev["gap"] >= 0:
        pros.append(f"市場予想は会社予想を{ev['cons_gap']:+.0f}%上回っており、進捗もそれを支えている。")
    for n in r.get("pos_news") or []:
        pros.append(f"{n['t'][:8]}：{n['h'][:46]}")
    # 注意点
    if rt is not None and rt >= 15:
        cons.append(f"信用倍率は{rt:.0f}倍で、まだ買いが多い（買い長）。")
    if not r["up"] and not r["above200"]:
        cons.append("75日線・200日線の下で、トレンドはまだ弱い。")
    if r["atrp"] >= 4:
        cons.append(f"値幅が{r['atrp']:.1f}%と大きく、損切り幅は{r['lv']['risk']:.1f}%。資金は少なめに。")
    if r["dev25"] >= 6:
        cons.append(f"25日線から{r['dev25']:+.1f}%上にあり、やや過熱。押し目を待つ。")
    if r["val20"] < 5:
        cons.append(f"売買代金は1日{r['val20']:.1f}億円と少なめ。")
    if ev.get("gap") is not None and ev["gap"] <= -3:
        cons.append(f"経常益の進捗率は{ev['prog']:.0f}%で、前年同期の{ev['prog_ly']:.0f}%を下回る。")
    if ev.get("cons_gap") is not None and ev["cons_gap"] >= 10 and (ev.get("gap") is None or ev["gap"] < 3):
        cons.append(f"市場予想が会社予想を{ev['cons_gap']:+.0f}%上回るが、進捗はそれに追いついていない。届かなければ失望売り。")
    if ev.get("pre20", 0) >= 10 and ev.get("bdEarn") is not None and ev["bdEarn"] <= 20:
        cons.append("決算前に大きく上げており、決算翌日は売られやすい（過去の上昇確率42%）。")
    if ev.get("bdEarn") is not None and ev["bdEarn"] <= 20:
        cons.append(f"決算は{ev['earn'][5:]}（あと{ev['bdEarn']}営業日）。{ev['action']}。")
    for n in r.get("neg_news") or []:
        cons.append(f"{n['t'][:8]}：{n['h'][:46]}")
    return pros[:6], cons[:6]
