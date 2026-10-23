"""機械的な売買ルールを、今の時刻に当てはまる分だけ実行する（仮想売買。実際の発注はしない）。

  python3 tools/auto.py          … 約定などを処理してから、ルールどおりに注文して作業用の記録に保存（publish はしない）
  python3 tools/auto.py --dry    … 注文せずに、何をするかだけ表示

時刻ごとのルール（日本時間・営業日）:
  8:25〜8:59   M1 朝の窓下げ買い：前日も下げ、夜間取引（PTS）で前日終値より1.5〜6%安い銘柄を、窓の大きい順に寄り付きで買う
               （PTS は株探から取る。株探は GitHub Actions からは取れないので、M1 は 8:40 の Claude の更新で動かす）
  9:00〜15:20  スイング：見立てが「買い候補」で、現在値が買い目安＋0.3ATR以内なら買う。利確1・利確2・20営業日・決算前の売り
  12:30〜13:20 P1 後場の強い銘柄買い：市場の前場が強い日に、前場で市場より強く高値圏で引けた銘柄を買う
デイトレードはどちらも大引けで自動決済（trade.py）。ルールの値は data/trade/day_rules.json で、15:30の更新で見直す。
8:40（朝）・12:30 の Claude の更新と、1時間ごとの確認（GitHub Actions、平日 10:07・11:07・13:37・14:37）で動かす。
"""
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trade as T  # noqa: E402

ROOT = T.ROOT
RULES = os.path.join(ROOT, "data", "trade", "day_rules.json")
NOTES = os.path.join(ROOT, "docs", "data", "notes.json")
DEFAULT_RULES = {
    "M1": {"on": True, "name": "朝の窓下げ買い", "gap_lo": -0.06, "gap_hi": -0.015, "prev_down": True, "max": 3, "stop": 0.05, "size": 0.35},
    "P1": {"on": True, "name": "後場の強い銘柄買い", "mkt": 0.003, "rel_lo": 0.01, "rel_hi": 0.06, "pos": 0.7, "max": 1, "stop": 0.03, "size": 0.5},
    "log": [],
}
SKIP_WORDS = ("TOB", "公開買付", "上場廃止", "監理", "整理銘柄", "株式併合", "MBO")


def rules():
    try:
        with open(RULES, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_RULES))


def hm(t):
    return (t.hour, t.minute)


def floor100(x):
    return int(max(0, x) // 100) * 100


def free_cash(pf, acct):
    return pf["accounts"][acct]["cash"] - T.reserved(pf, acct)


def held(pf, acct):
    return {p["code"] for p in pf["positions"] if p["acct"] == acct} | {o["code"] for o in pf["orders"] if o["acct"] == acct and o["side"] == "buy"}


def bad_news(code, hours=20):
    import sources as S
    try:
        ns = S.kabutan_news(code, 6)
    except Exception:
        return None
    lim = T.now() - timedelta(hours=hours)
    for n in ns:
        try:
            tm = datetime.strptime(n["t"][:14], "%y/%m/%d %H:%M").replace(tzinfo=T.JST)
        except ValueError:
            continue
        if tm >= lim and any(w in n["h"] for w in SKIP_WORDS):
            return n["h"]
    return None


def pts(code):
    """株探の PTS（夜間・朝の私設取引）の値段と時刻。取れなければ None。"""
    import sources as S
    h = S.get(f"https://kabutan.jp/stock/?code={code}")
    if not h:
        return None
    tx = re.sub(r"<[^>]+>", " ", h)
    m = re.search(r"PTS\s+([\d,]+(?:\.\d+)?)\s*円\s+(\d{2}):(\d{2})\s+(\d{2})/(\d{2})", tx)
    if not m:
        return None
    t = T.now()
    mo, d = int(m.group(4)), int(m.group(5))
    y = t.year - 1 if mo > t.month + 1 else t.year
    return float(m.group(1).replace(",", "")), datetime(y, mo, d, int(m.group(2)), int(m.group(3)), tzinfo=T.JST)


def say(out, s):
    out.append(s)
    print(s)


# ---------------------------------------------------------------- M1 朝の窓下げ買い
def rule_m1(pf, R, t, dry, out):
    if not R.get("on"):
        say(out, "M1 朝の窓下げ買い：停止中")
        return
    codes = list(T.DAY_LIST)
    D = T.daily(codes)
    prev_day = T.trading_on_or_before(t.date() - timedelta(days=1))
    pre = []
    for c in codes:
        x = D.get(c)
        if x is None:
            continue
        x = x[x.index.date <= prev_day]
        if len(x) < 3 or x.index[-1].date() != prev_day:
            continue
        pc, ppc = float(x.Close.iloc[-1]), float(x.Close.iloc[-2])
        if R.get("prev_down") and pc >= ppc:
            continue
        pre.append((c, pc, pc / ppc - 1))
    since = datetime(prev_day.year, prev_day.month, prev_day.day, 15, 30, tzinfo=T.JST)
    cand = []
    for c, pc, pr in pre:
        q = pts(c)
        if not q or q[1] <= since:
            continue
        g = q[0] / pc - 1
        if R["gap_lo"] <= g <= R["gap_hi"]:
            cand.append(dict(code=c, pc=pc, pr=pr, pts=q[0], pts_t=q[1], gap=g))
    cand.sort(key=lambda r: r["gap"])
    say(out, f"M1 朝の窓下げ買い：前日も下げた{len(pre)}銘柄のうち、PTSで{R['gap_lo'] * 100:.1f}〜{R['gap_hi'] * 100:.1f}%安いのは{len(cand)}銘柄")
    have = held(pf, "day")
    eq = pf["accounts"]["day"].get("equity", T.CAPITAL)
    d0 = t.strftime("%Y-%m-%d")
    n = sum(1 for o in pf["orders"] if o.get("rule") == "M1" and o["time"][:10] == d0) +         sum(1 for p in pf["positions"] if p.get("rule") == "M1" and p["opened"][:10] == d0)
    if n:
        say(out, f"  今日の M1 はすでに{n}銘柄を注文済み")
    for r in cand:
        if n >= R["max"]:
            break
        if r["code"] in have:
            continue
        if len(have) >= T.CAP["day"]:
            say(out, "  デイトレードの保有上限に達したので終了")
            break
        nb = bad_news(r["code"])
        if nb:
            say(out, f"  {r['code']} {T.DAY_LIST[r['code']]}：ニュース「{nb}」のため見送り")
            continue
        per = min(free_cash(pf, "day"), eq * R.get("size", 0.35))
        sh = floor100(per / (r["pts"] * T.BUF))
        if sh <= 0:
            say(out, f"  {r['code']} {T.DAY_LIST[r['code']]}：100株でも1枠（{per:,.0f}円）を超えるため見送り")
            continue
        reason = (f"朝の窓下げ買い（M1）：前日{r['pr'] * 100:+.1f}%に続き、PTSで前日終値より{r['gap'] * 100:+.1f}%安い"
                  f"（{r['pts_t']:%H:%M}）。下げて始まった分の戻りを狙い、寄り付きで買って大引けで売る。損切りは買値−{R['stop'] * 100:.0f}%。")
        say(out, f"  買い {r['code']} {T.DAY_LIST[r['code']]} {sh}株（PTS {r['pts']:,.1f}円）")
        if not dry:
            T.cmd_buy(pf, "day", r["code"], sh, reason, rule="M1", stop_pct=R["stop"], est=r["pts"], name=T.DAY_LIST[r["code"]], quiet=True)
        have.add(r["code"])
        n += 1


# ---------------------------------------------------------------- P1 後場の強い銘柄買い
def rule_p1(pf, R, t, dry, out):
    import pandas as pd
    if not R.get("on"):
        say(out, "P1 後場の強い銘柄買い：停止中")
        return
    codes = list(T.DAY_LIST) + ["1306"]
    x = T._yf(codes, period="1d", interval="5m")
    rows, mk = [], None
    for c, g in x.items():
        g.index = pd.DatetimeIndex(g.index).tz_convert(T.JST)
        g = g[g.index.date == t.date()]
        h = g.index.strftime("%H:%M")
        mor = g[h < "11:30"]
        if not len(mor) or h[0] > "09:05" or mor.index[-1].strftime("%H:%M") < "11:20":
            continue
        op, mc, hi, lo = float(mor.Open.iloc[0]), float(mor.Close.iloc[-1]), float(mor.High.max()), float(mor.Low.min())
        r = dict(code=c, mret=mc / op - 1, mpos=(mc - lo) / (hi - lo) if hi > lo else 0.5, last=float(g.Close.iloc[-1]))
        if c == "1306":
            mk = r
        else:
            rows.append(r)
    if mk is None:
        say(out, "P1 後場の強い銘柄買い：市場（1306）の前場の値が取れないため判断しない")
        return
    ok = mk["mret"] >= R["mkt"]
    say(out, f"P1 後場の強い銘柄買い：市場（1306）の前場 {mk['mret'] * 100:+.2f}%（条件 +{R['mkt'] * 100:.1f}%以上）→ "
             + ("条件を満たす" if ok else "条件を満たさないので見送り"))
    if not ok:
        return
    for r in rows:
        r["rel"] = r["mret"] - mk["mret"]
    cand = sorted([r for r in rows if R["rel_lo"] <= r["rel"] <= R["rel_hi"] and r["mpos"] >= R["pos"]], key=lambda r: -r["rel"])
    say(out, f"  候補（市場比 {R['rel_lo'] * 100:+.0f}〜{R['rel_hi'] * 100:+.0f}%・高値圏{R['pos'] * 100:.0f}%以上）: "
             + (" / ".join(f"{r['code']} {T.DAY_LIST[r['code']]} 市場比{r['rel'] * 100:+.1f}%" for r in cand[:5]) or "なし"))
    have = held(pf, "day")
    eq = pf["accounts"]["day"].get("equity", T.CAPITAL)
    n = 0
    for r in cand:
        if n >= R["max"]:
            break
        if r["code"] in have or len(have) >= T.CAP["day"]:
            continue
        nb = bad_news(r["code"])
        if nb:
            say(out, f"  {r['code']} {T.DAY_LIST[r['code']]}：ニュース「{nb}」のため見送り")
            continue
        per = min(free_cash(pf, "day"), eq * R.get("size", 0.5))
        sh = floor100(per / (r["last"] * T.BUF))
        if sh <= 0:
            say(out, f"  {r['code']} {T.DAY_LIST[r['code']]}：100株でも1枠（{per:,.0f}円）を超えるため見送り")
            continue
        reason = (f"後場の強い銘柄買い（P1）：市場の前場が{mk['mret'] * 100:+.2f}%と強い日に、前場で市場より{r['rel'] * 100:+.1f}%強く、"
                  f"値幅の{r['mpos'] * 100:.0f}%の高値圏で引けた。後場も強さが続くのを狙い、大引けで売る。損切りは買値−{R['stop'] * 100:.0f}%。")
        say(out, f"  買い {r['code']} {T.DAY_LIST[r['code']]} {sh}株（{r['last']:,.1f}円）")
        if not dry:
            T.cmd_buy(pf, "day", r["code"], sh, reason, rule="P1", stop_pct=R["stop"], est=r["last"], name=T.DAY_LIST[r["code"]], quiet=True)
        have.add(r["code"])
        n += 1


# ---------------------------------------------------------------- スイング
def load_screen():
    try:
        with open(T.LATEST, encoding="utf-8") as f:
            L = json.load(f)
        with open(NOTES, encoding="utf-8") as f:
            N = json.load(f)
    except (OSError, ValueError):
        return None, {}
    views = N.get("views", {}) if str(N.get("updated", ""))[:16] == L["meta"]["updated"][:16] else {}
    return L, views


def rule_swing(pf, t, dry, out, B):
    t0 = t.date()
    # 売り（利確・期間・決算前）
    for p in [p for p in pf["positions"] if p["acct"] == "swing"]:
        last = p.get("last") or p["avg"]
        pend = sum(o["shares"] for o in pf["orders"] if o["side"] == "sell" and o["acct"] == "swing" and o["code"] == p["code"])
        free = p["shares"] - pend
        if free <= 0:
            continue
        act = None
        if p.get("tp2") and last >= p["tp2"]:
            act = (free, "tp2", f"利確2（{p['tp2']:,.0f}円）に届いたので残りを全部売る。")
        elif p.get("tp1") and last >= p["tp1"] and not p.get("tp1_done"):
            if p["shares"] >= 200:
                act = (floor100(p["shares"] / 2), "tp1", f"利確1（{p['tp1']:,.0f}円）に届いたので半分を売り、残りの損切りを買値まで上げる。")
            else:
                act = (free, "tp1", f"利確1（{p['tp1']:,.0f}円）に届いた。100株なので全部売る。")
        elif p.get("earn") and p.get("hold") is not None and p["hold"] < 1 and not p.get("earn_done"):
            e = datetime.strptime(p["earn"], "%Y-%m-%d").date()
            if e >= t0 and T.bdays_until(t0, e) <= 1:
                sh = free if p["hold"] == 0 or p["shares"] < 200 else floor100(p["shares"] / 2)
                act = (sh, "earn", f"決算（{e:%m/%d}）の前営業日。決算予想の判定（{'全部売る' if p['hold'] == 0 else '半分以上売る'}）に従って売る。")
        elif not p.get("tp1_done") and T.bdays_until(datetime.strptime(p["opened"][:10], "%Y-%m-%d").date(), t0) >= 20:
            act = (free, "time", "買ってから20営業日たっても利確1に届かないので売る。")
        if not act:
            continue
        sh, how, why = act
        sh = min(sh, free)
        say(out, f"スイング 売り {p['code']} {p['name']} {sh}株：{why}")
        if dry:
            continue
        T.cmd_sell(pf, "swing", p["code"], sh, why, how=how, quiet=True)
        if how == "tp1" and sh < p["shares"]:
            p["tp1_done"] = True
            if (p.get("stop") or 0) < p["avg"] and p["avg"] < last:
                T.cmd_stop(pf, "swing", p["code"], math.floor(p["avg"]), "利確1で半分を売ったので、残りの損切りを買値に上げる", quiet=True)
        if how == "earn":
            p["earn_done"] = True
    # 買い
    L, views = load_screen()
    if L is None:
        say(out, "スイング：予想のデータが読めないため買わない")
        return
    up = datetime.strptime(L["meta"]["updated"][:10], "%Y-%m-%d").date()
    if T.bdays_until(up, t0) > 1:
        say(out, f"スイング：予想が古い（{up:%m/%d}）ため買わない")
        return
    k = L["market"]
    if k["n225"] < k["stop_line"]:
        say(out, f"スイング：日経平均（{k['n225']:,.0f}）が新規買いの停止ライン（{k['stop_line']:,.0f}）を下回っているため買わない")
        return
    if not views:
        say(out, "スイング：今回の見立て（notes.json）がまだ無いため買わない")
        return
    ps = sorted([s for s in L["stocks"] if s["grade"] in ("本命", "次点") and views.get(s["code"]) == "買い候補"], key=lambda s: -s["ev"])
    have = held(pf, "swing")
    if not ps:
        say(out, "スイング：見立てが「買い候補」の銘柄なし")
        return
    need = [s["code"] for s in ps if s["code"] not in have]
    px = T.bars(need) if need else {}
    acc = pf["accounts"]["swing"]
    eq = acc.get("equity", T.CAPITAL)
    for s in ps:
        c, lv, e = s["code"], s["lv"], s.get("earnv") or {}
        if c in have:
            continue
        if len(have) >= T.CAP["swing"]:
            say(out, "スイング：保有上限に達した")
            break
        if e.get("bdEarn") is not None and e["bdEarn"] <= 10 and (e.get("hold") is None or e["hold"] < 1):
            say(out, f"スイング：{c} {s['name']} は決算まで{e['bdEarn']}営業日で決算前に売る判定のため買わない")
            continue
        x = px.get(c)
        if x is None or not len(x):
            continue
        cur = float(x.Close.iloc[-1])
        lim = lv["entry"] + 0.3 * s["atr"]
        if cur > lim:
            say(out, f"スイング：{c} {s['name']} 現在 {cur:,.0f}円は買い目安 {lv['entry']:,}円＋0.3ATR（{lim:,.0f}円）より上なので待つ")
            continue
        if cur <= lv["stop"]:
            say(out, f"スイング：{c} {s['name']} 現在 {cur:,.0f}円は損切りライン以下のため買わない")
            continue
        sh = floor100(min(0.02 * eq / (cur - lv["stop"]), 0.3 * eq / cur, free_cash(pf, "swing") / (cur * T.BUF)))
        if sh <= 0:
            say(out, f"スイング：{c} {s['name']} は100株でも資金の上限（損失2%・金額30%）を超えるため見送り")
            continue
        reason = (f"信用の買い残が減った{s['grade']}（{'需給改善型' if s['group'] == 'main' else '投げ一巡型'}）で、見立ては買い候補。"
                  f"現在値が買い目安（{lv['entry']:,}円）付近まで下がった。損切り{lv['stop']:,}円・利確1 {lv['tp1']:,}円・利確2 {lv['tp2']:,}円。"
                  + (f"決算（{e['earn'][5:]}）は「{e['action']}」の判定。" if e.get("earn") else ""))
        say(out, f"スイング 買い {c} {s['name']} {sh}株（現在 {cur:,.0f}円）")
        if not dry:
            T.cmd_buy(pf, "swing", c, sh, reason, stop=lv["stop"], tp1=lv["tp1"], tp2=lv["tp2"], rule="S1", est=cur, name=s["name"],
                      earn=(e.get("earn") or "").replace("/", "-") or None, hold=e.get("hold"), quiet=True)
        have.add(c)


def probe():
    """GitHub Actions などから、株価（Yahoo）と株探（PTS）が取れるかだけ確かめる。"""
    import sources as S
    gh = bool(os.environ.get("GITHUB_ACTIONS"))

    def note(s):            # GitHub Actions では注記（annotation）にも出す（ログを見る権限がなくても読める）
        print(("::notice::" if gh else "") + s)
    ok = True
    try:
        x = T.bars(["7203"]).get("7203")
        note("Yahoo 1分足: " + ("取れない" if x is None or not len(x) else f"{len(x)}本・最後 {x.index[-1]:%m/%d %H:%M}"))
        ok &= x is not None and len(x) > 0
    except Exception as e:
        note(f"Yahoo 1分足: エラー {type(e).__name__}: {e}"[:300])
        ok = False
    try:
        d = T.daily(["7203"]).get("7203")
        note("Yahoo 日足: " + ("取れない" if d is None or not len(d) else f"最後 {d.index[-1]:%m/%d} 終値 {float(d.Close.iloc[-1]):,.1f}"))
        ok &= d is not None and len(d) > 0
    except Exception as e:
        note(f"Yahoo 日足: エラー {type(e).__name__}: {e}"[:300])
        ok = False
    try:
        h = S.get("https://kabutan.jp/stock/?code=7203")
        q = pts("7203")
        note(f"株探: ページ {'取れた（' + str(len(h)) + '文字）' if h else '取れない'}・PTS {q if q else '取れない'}")
        ok &= bool(q)
    except Exception as e:
        note(f"株探: エラー {type(e).__name__}: {e}"[:300])
        ok = False
    return 0 if ok else 1


def main(argv):
    if "--probe" in argv:
        return probe()
    dry = "--dry" in argv
    t = T.now()
    if not T.is_trading(t.date()):
        print("休場日のため何もしない")
        return 0
    if not ((8, 25) <= hm(t) < (15, 20)):
        print(f"{t:%H:%M} は売買ルールの時間外（8:25〜15:20）")
        return 0
    pf = T.load()
    R = rules()
    B, D = T.process(pf)
    T.value(pf, B, D)
    out = []
    if (8, 25) <= hm(t) < (8, 59):
        rule_m1(pf, R["M1"], t, dry, out)
    if (9, 0) <= hm(t) < (15, 20):
        rule_swing(pf, t, dry, out, B)
    if (12, 30) <= hm(t) < (13, 20):
        rule_p1(pf, R["P1"], t, dry, out)
    if not dry:
        T.value(pf, B, D)
        pf["auto"] = {"t": t.strftime("%Y-%m-%d %H:%M"), "lines": out[-30:]}
        T.save(pf)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except T.TradeError as e:
        print("ERROR: " + str(e), file=sys.stderr)
        sys.exit(1)
