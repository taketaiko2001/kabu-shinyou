"""買い残減少スクリーナー：毎回の更新を行う本体。

使い方:  python tools/run.py            （休場日は何もしない）
         python tools/run.py --force    （休場日でも実行）
出力:    docs/data/latest.json, docs/data/track.json, docs/data/history/*.json, data/margin/*.csv.gz
失敗したときは何も書き換えずに終了コード1で終わる。
"""
import gzip
import io
import json
import os
import re
import sys
import warnings
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import model as M  # noqa: E402
import sources as S  # noqa: E402

warnings.filterwarnings("ignore")   # yfinance/pandas の大量の警告で出力が埋まるのを防ぐ

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")
SNAP = os.path.join(ROOT, "data", "margin")
JST = timezone(timedelta(hours=9))
NOW = datetime.now(JST)
TODAY = pd.Timestamp(NOW.date())

EVIDENCE = dict(
    margin=[
        dict(k="買い残増（+15%以上）× 株価下落", n=608, x20=-3.95, win=33.7, note="比較用：ナンピン買いが積み上がる悪い形"),
        dict(k="買い残減（-10%以下）× 取組が軽い（出来高1日分未満）", n=1825, x20=-2.34, ev=0.53, note="大型株に多い。減っても効かない"),
        dict(k="全イベント（基準）", n=12191, x20=-0.82, win=42.0, ev=0.81, note="220銘柄・約1年の全週"),
        dict(k="買い残減 × 取組が重い × 株価横ばい〜上昇（本命型）", n=234, x20=1.09, win=58.6, ev=1.03, note="損切41%・利確2到達34%"),
        dict(k="買い残減（-10%以下）× 取組が重い（1日分以上）", n=300, x20=2.81, win=61.3, ev=1.63, note="本命型＋投げ一巡型の合計"),
        dict(k="買い残減 × 取組が重い × 株価下落（投げ一巡型）", n=46, x20=8.09, win=73.9, ev=4.58, note="件数が少なく参考値"),
    ],
    earnings=[
        dict(k="決算前20日に日経平均を10%以上上回った", n=161, react=0.30, med=-1.54, up=41.6, post=3.96),
        dict(k="決算前20日の値動きが日経平均並み（±3%）", n=362, react=0.11, med=-0.10, up=49.7, post=0.94),
        dict(k="決算前20日に日経平均を10%以上下回った", n=269, react=0.93, med=0.22, up=50.6, post=-2.47),
        dict(k="前の四半期の経常益が前年比+50%以上", n=123, react=1.45, med=1.74, up=56.1, post=-0.30),
        dict(k="好業績（前四半期+20%以上）で決算前の上昇が小さい", n=208, react=1.05, med=None, up=55.8, post=None),
        dict(k="好業績だが決算前に大きく上昇済み", n=43, react=-0.84, med=None, up=41.9, post=None),
    ],
    earn_note="189銘柄・1,334回の決算（2024年11月〜2026年10月）。発表前日の終値から翌営業日の終値までの、日経平均に対する値動き。平均の値幅は±6.0%。",
)


# ---------------------------------------------------------------- 営業日
def _holidays(y0, y1):
    hs = set()
    try:
        import jpholiday
        for y in range(y0, y1 + 1):
            for d, _ in jpholiday.year_holidays(y):
                hs.add(pd.Timestamp(d))
    except Exception:
        pass
    for y in range(y0, y1 + 1):
        for md in ("01-01", "01-02", "01-03", "12-31"):
            hs.add(pd.Timestamp(f"{y}-{md}"))
    return hs


HOL = _holidays(NOW.year - 1, NOW.year + 1)


def is_trading(d):
    d = pd.Timestamp(d).normalize()
    return d.weekday() < 5 and d not in HOL


def trading_days(a, b):
    """a以上b未満の営業日の数。"""
    if b <= a:
        return 0
    return sum(1 for d in pd.date_range(a, b - pd.Timedelta(days=1)) if is_trading(d))


def prev_trading(d):
    d = pd.Timestamp(d) - pd.Timedelta(days=1)
    while not is_trading(d):
        d -= pd.Timedelta(days=1)
    return d


# ---------------------------------------------------------------- 補助
def jnum(v, d=2):
    if v is None:
        return None
    try:
        if isinstance(v, (float, np.floating)) and (np.isnan(v) or np.isinf(v)):
            return None
    except Exception:
        pass
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return round(float(v), d)
    return v


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (bool, np.bool_)):
        return bool(o)
    if isinstance(o, (int, float, np.integer, np.floating)):
        return jnum(o)
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.strftime("%Y-%m-%d")
    return o


def save_snapshot(date, df):
    os.makedirs(SNAP, exist_ok=True)
    fn = os.path.join(SNAP, f"{date}.csv.gz")
    if not os.path.exists(fn):
        df[["code", "buy", "sell"]].to_csv(fn, compression="gzip")


def record_bad(bad):
    os.makedirs(SNAP, exist_ok=True)
    fn = os.path.join(SNAP, "bad_dates.json")
    old = json.load(open(fn, encoding="utf-8")) if os.path.exists(fn) else []
    new = sorted(set(old) | set(bad))
    with open(fn, "w", encoding="utf-8") as f:
        json.dump(new, f)
    return new


def snapshot_before(date_t, n=20):
    """date_tからn営業日以上前の、もっとも新しいスナップショット。"""
    if not os.path.isdir(SNAP):
        return None, None
    t = pd.Timestamp(date_t)
    best = None
    fn = os.path.join(SNAP, "bad_dates.json")
    bad = set(json.load(open(fn, encoding="utf-8"))) if os.path.exists(fn) else set()
    for f in sorted(os.listdir(SNAP)):
        m = re.match(r"(\d{8})\.csv\.gz$", f)
        if m and m.group(1) not in bad and trading_days(pd.Timestamp(m.group(1)), t) >= n:
            best = m.group(1)
    if not best:
        return None, None
    return best, pd.read_csv(os.path.join(SNAP, f"{best}.csv.gz"), index_col=0, dtype={"code": str})


GENERIC = ("本日の【", "今週の【", "前日に動いた銘柄", "株探トップ特集", "成長株特集", "割安株特集", "レーティング日報",
           "注目個別銘柄", "動意株", "銘柄ダイジェスト", "話題株ダイジェスト", "明日の株式相場", "相場観測", "相場展望",
           "マーケット・フォーキャスト", "日本取引所、", "CHART CLUB")


def news_recent(news, days=14):
    out = []
    for n in news:
        if any(g in n.get("h", "") for g in GENERIC):
            continue
        m = re.match(r"(\d{2})/(\d{2})/(\d{2})", n.get("t", ""))
        if m:
            d = pd.Timestamp(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if (TODAY - d).days <= days:
                out.append(n)
    return out


# ---------------------------------------------------------------- 成績の記録
def simulate(p, x):
    """記録した1件の予想を、その後の株価で判定する。"""
    created = pd.Timestamp(p["created"])
    bars = x[x.index > created]
    if bars.empty:
        return p
    e, sl, t1, t2 = p["entry"], p["stop"], p["tp1"], p["tp2"]
    if p["status"] == "waiting":
        fill = None
        for i, (d, b) in enumerate(bars.iterrows()):
            if i >= 5:
                break
            if b.Low <= e:
                fill = (d, min(b.Open, e))
                break
        if fill is None:
            if len(bars) >= 5:
                p.update(status="expired", note="5営業日で買い目安に届かず")
            return p
        p.update(status="open", fill_date=fill[0].strftime("%Y-%m-%d"), fill=float(fill[1]))
    fd = pd.Timestamp(p["fill_date"])
    after = x[x.index >= fd]
    en = p["fill"]
    half, out, why = None, None, ""
    for i, (d, b) in enumerate(after.iterrows()):
        if i == 0:
            if b.Close <= sl:
                out, why = sl, "損切り"
                break
            continue
        if half is None:
            if b.Low <= sl:
                out, why = min(b.Open, sl), "損切り"
                break
            if b.High >= t1:
                half = t1
                if b.High >= t2:
                    out, why = t2, "利確2"
                    break
        else:
            if b.Low <= en:
                out, why = min(b.Open, en), "利確1後に建値"
                break
            if b.High >= t2:
                out, why = t2, "利確2"
                break
        if i >= 20:
            out, why = b.Close, "20営業日で手仕舞い"
            break
    last = float(after.Close.iloc[-1])
    if out is not None:
        pnl = ((half + out) / 2 / en - 1) * 100 if half is not None else (out / en - 1) * 100
        p.update(status="closed", exit_date=d.strftime("%Y-%m-%d"), pnl=round(pnl, 2), note=why)
    else:
        pnl = ((half + last) / 2 / en - 1) * 100 if half is not None else (last / en - 1) * 100
        p.update(status="open", pnl=round(pnl, 2), note="利確1済み" if half is not None else "保有中")
    return p


def update_track(picks, px):
    fn = os.path.join(DATA, "track.json")
    log = json.load(open(fn, encoding="utf-8")) if os.path.exists(fn) else []
    active = {p["code"] for p in log if p["status"] in ("waiting", "open")}
    for s in picks:
        if s["grade"] in ("本命", "次点") and s["code"] not in active:
            log.append(dict(id=f"{TODAY:%Y%m%d}_{s['code']}", code=s["code"], name=s["name"], group=s["group"],
                            grade=s["grade"], created=f"{TODAY:%Y-%m-%d}", entry=s["lv"]["entry"], stop=s["lv"]["stop"],
                            tp1=s["lv"]["tp1"], tp2=s["lv"]["tp2"], status="waiting"))
    need = [p["code"] for p in log if p["status"] in ("waiting", "open") and p["code"] not in px]
    if need:
        px.update(S.prices(sorted(set(need)), period="6mo"))
    for p in log:
        if p["status"] in ("waiting", "open") and p["code"] in px:
            simulate(p, px[p["code"]])
    closed = [p for p in log if p["status"] == "closed"]
    summ = dict(closed=len(closed), wins=sum(1 for p in closed if p["pnl"] > 0),
                avg=round(float(np.mean([p["pnl"] for p in closed])), 2) if closed else None,
                open=sum(1 for p in log if p["status"] == "open"),
                waiting=sum(1 for p in log if p["status"] == "waiting"),
                expired=sum(1 for p in log if p["status"] == "expired"))
    return log, summ


# ---------------------------------------------------------------- 本体
def main(force=False):
    if not force and not is_trading(TODAY):
        print(f"{TODAY:%Y-%m-%d} は休場日のため更新しません。")
        return 0
    slot = "寄り付き前" if NOW.hour < 11 else "前引け後" if NOW.hour < 15 or (NOW.hour == 15 and NOW.minute < 30) else "大引け後"

    # 1. 信用残（日次・全銘柄）
    files = S.jpx_margin_files()
    if len(files) < 5:
        print("ERROR: JPXの日次信用残ファイルが取得できませんでした。", len(files))
        return 1
    parsed = []
    for d, u in files[:5]:
        raw = S.get(u, binary=True)
        x = S.parse_jpx_pdf(raw) if raw else None
        if x is not None and len(x) >= 3000:
            parsed.append((d, x))
    if len(parsed) < 2:
        print("ERROR: 信用残PDFのダウンロード・解析に失敗。", len(parsed))
        return 1
    # 市場全体の買い残が前日から8%以上動いた日は、データの欠落・誤りとみなして使わない
    bad = []
    for d, x in parsed:
        tot, chg = x.buy.sum(), x.buy_chg.sum()
        if abs(chg) / max(1, tot - chg) > 0.08:
            bad.append(d)
    good = [(d, x) for d, x in parsed if d not in bad]
    if len(good) < 2:
        print("ERROR: 正常な信用残ファイルが2日分以上ありません。異常日:", bad)
        return 1
    (date_t, mt), (date_b, mb) = good[0], good[-1]
    for d, x in good:
        save_snapshot(d, x)
    d_to = pd.Timestamp(date_t)
    d_from = prev_trading(pd.Timestamp(date_b))
    if bad:
        print("注意: 次の日の信用残は市場全体で前日比8%超の異常な変化のため除外:", bad)
    rate_days = trading_days(d_from, d_to + pd.Timedelta(days=1)) - 1
    df = mt[["code", "name", "mkt", "kind", "buy", "sell"]].copy()
    df["buy0"] = mb.buy - mb.buy_chg
    df["sell0"] = mb.sell - mb.sell_chg
    df = df.dropna(subset=["buy", "buy0"])
    df["rate"] = (df.buy / df.buy0 - 1) * 100
    record_bad(bad)
    snap_date, snap = snapshot_before(date_t, 20)
    uni = df[(df.buy0 >= 100000) & (df.rate <= -10)]
    print(f"信用残: {date_t}（比較 {d_from:%Y%m%d}）全{len(df)}銘柄 / 減少候補 {len(uni)}")

    # 2. 株価
    px = S.prices(list(uni.code), period="2y")
    n225 = S.index_n225("2y")
    if n225 is None or len(px) < len(uni) * 0.6:
        print("ERROR: 株価の取得に失敗。", len(px), "/", len(uni))
        return 1
    last_bar = max(x.index[-1] for x in px.values())
    partial = (pd.Timestamp(last_bar).normalize() == TODAY) and (NOW.hour < 15 or (NOW.hour == 15 and NOW.minute < 30))
    if partial and slot == "寄り付き前":   # 手動で場中に動かしたとき
        slot = "場中"
    warn = []
    n_last = pd.Timestamp(n225.index[-1]).normalize()
    if n_last < pd.Timestamp(last_bar).normalize():
        warn.append(f"日経平均が{n_last:%m/%d}までしか取れていない（株価は{pd.Timestamp(last_bar):%m/%d}）")
        print("注意: " + warn[-1])

    rows = []
    for code5, m in uni.iterrows():
        x = px.get(m.code)
        if x is None:
            continue
        t = M.technicals(x, n225, d_from, d_to, partial)
        if t is None or not t["vol20"]:
            continue
        r = dict(code=m.code, name=m["name"], mkt=m.mkt, rate=m.rate, rate_days=rate_days,
                 buy0=m.buy0, buy1=m.buy, sell0=m.sell0, sell1=m.sell,
                 ratio0=m.buy0 / m.sell0 if m.sell0 > 0 else None, ratio1=m.buy / m.sell if m.sell > 0 else None,
                 sell_chg=(m.sell / m.sell0 - 1) * 100 if m.sell0 > 0 else None,
                 rate4=None, rate4_src="")
        if snap is not None and code5 in snap.index and snap.loc[code5, "buy"] > 0:
            r["rate4"], r["rate4_src"] = (m.buy / snap.loc[code5, "buy"] - 1) * 100, f"{snap_date}比"
        r.update(t)
        r["dc0"], r["dc1"] = m.buy0 / t["vol20"], m.buy / t["vol20"]
        r["group"] = M.classify(r)
        if r["group"]:
            r["score"] = M.score(r)
            rows.append(r)
    rows.sort(key=lambda r: -r["score"])
    cand = [r for r in rows if r["group"] == "main"][:28] + [r for r in rows if r["group"] == "rebound"][:12]
    print(f"条件を満たした候補: {len(rows)}（詳細を調べる {len(cand)}）")

    # 3. 候補ごとの業績・決算・ニュース
    def fetch_detail(c):
        return c, dict(top=S.kabutan_top(c), hist=S.kabutan_margin_hist(c), news=S.kabutan_news(c),
                       fin=S.kabutan_finance(c), cons=S.kabuyoho(c))

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as ex:
        detail = dict(ex.map(fetch_detail, [r["code"] for r in cand]))
    picks = []
    for r in cand:
        c = r["code"]
        dt = detail[c]
        top, hist = dt["top"], dt["hist"]
        hist = [h for h in hist if h["d"] not in {f"{b[2:4]}/{b[4:6]}/{b[6:]}" for b in bad}]
        if r["rate4"] is None and len(hist) >= 10 and hist[0]["b"]:
            r["rate4"], r["rate4_src"] = (r["buy1"] / hist[0]["b"] - 1) * 100, f"{hist[0]['d'][3:]}比"
        r["group"] = M.classify(r)
        if not r["group"]:
            continue
        news = news_recent(dt["news"], 14)
        fin, cons = dt["fin"], dt["cons"]
        earn = top.get("earn", "")
        bd = None
        if earn:
            ed = pd.Timestamp(earn.replace("/", "-"))
            bd = trading_days(TODAY, ed) if ed >= TODAY else None
        r["earnv"] = M.earnings_view(fin, cons, px[c], n225, earn if bd is not None else "", bd, r["pre20"], r["price"])
        r["pos_news"], r["neg_news"] = M.news_flags(news)
        r["lv"] = M.levels(r)
        r["score"] = M.score(r)
        r["grade"] = M.grade(r)
        ev = r["earnv"]
        r["ev_tech"] = M.EV_ATR[r["group"]] * r["atrp"]
        r["ev_earn"] = M.p_open(ev.get("bdEarn")) * ev.get("hold", 0) * ev.get("Er", 0)
        r["ev"] = r["ev_tech"] + r["ev_earn"]
        r.update({k: top.get(k, "") for k in ("per", "pbr", "yld", "desc", "sector")})
        r["news"] = news[:5]
        badk = {f"{b[2:4]}/{b[4:6]}/{b[6:]}" for b in bad}
        r["mh"] = [h for h in hist if h["d"] not in badk][-25:]
        x = px[c].iloc[-60:]
        r["dates"] = [d.strftime("%m/%d") for d in x.index]
        r["close"] = [round(float(v), 1) for v in x.Close]
        picks.append(r)
    gr = {"本命": 0, "次点": 1, "監視": 2}
    picks.sort(key=lambda r: (r["group"] != "main", gr[r["grade"]], -r["score"]))
    main_ = [r for r in picks if r["group"] == "main"][:14]
    reb = [r for r in picks if r["group"] == "rebound"][:8]
    final = main_ + reb
    n225c = n225.Close
    for r in final:
        r["pros"], r["cons"] = M.reasons(r, None)

    # 4. 決算カレンダー（調べた候補のうち15営業日以内）
    cal = sorted([dict(code=r["code"], name=r["name"], earn=r["earnv"]["earn"], bdEarn=r["earnv"]["bdEarn"],
                       P=r["earnv"]["P"], Er=r["earnv"]["Er"], M=r["earnv"]["M"], label=r["earnv"]["label"],
                       action=r["earnv"]["action"], inList=r in final)
                  for r in picks if r["earnv"].get("bdEarn") is not None and r["earnv"]["bdEarn"] <= 15],
                 key=lambda d: d["bdEarn"])

    # 5. 市況
    mk = S.jpx_market_total()
    lows = n225.Low.iloc[-11:-1] if partial else n225.Low.iloc[-10:]
    market = dict(n225=float(n225c.iloc[-1]), n225_prev=float(n225c.iloc[-2]), n225_hi20=float(n225c.iloc[-20:].max()),
                  stop_line=float(lows.min()), n225_asof=f"{n_last:%Y-%m-%d}", total=mk)

    # 6. 成績の記録
    log, summ = update_track(final, px)

    meta = dict(updated=NOW.strftime("%Y-%m-%d %H:%M"), slot=slot, margin_date=date_t, margin_from=f"{d_from:%Y%m%d}",
                rate_days=rate_days, price_asof=f"{pd.Timestamp(last_bar):%Y-%m-%d}" + ("（場中）" if partial else ""),
                screened=len(df), universe=len(uni), passed=len(rows), studied=len(picks),
                ev_atr=M.EV_ATR, snap_4w=snap_date or "", excluded=bad, warn=warn)
    keep = ("code name mkt sector desc per pbr yld group grade score price prev atr atrp rate rate_days rate4 rate4_src pchg rs rsi dev25 "
            "ma25 ma75 ma200 up above200 buy0 buy1 sell0 sell1 ratio0 ratio1 dc0 dc1 val20 lv earnv ev ev_tech ev_earn "
            "pros cons news mh dates close").split()
    out = dict(meta=meta, market=market, stocks=[{k: r.get(k) for k in keep} for r in final], calendar=cal,
               evidence=EVIDENCE, track=dict(summary=summ, recent=[p for p in log][-30:][::-1]))
    js = json.dumps(clean(out), ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(DATA, "latest.json"), "w", encoding="utf-8") as f:   # 過去の版はGitの履歴に残る
        f.write(js)
    with open(os.path.join(DATA, "track.json"), "w", encoding="utf-8") as f:
        json.dump(clean(log), f, ensure_ascii=False, indent=0)

    # 7. ルーティン用の要約（標準出力）
    print(f"更新: {meta['updated']}（{slot}）株価 {meta['price_asof']} / 日経平均 {market['n225']:,.0f}（{n_last:%m/%d}）"
          f" 停止ライン {market['stop_line']:,.0f}")
    for r in final:
        e = r["earnv"]
        print(f"[{r['group']}/{r['grade']}] {r['code']} {r['name'][:10]} 現{r['price']:,.0f} 買{r['lv']['entry']:,} "
              f"損{r['lv']['stop']:,} 利{r['lv']['tp1']:,}/{r['lv']['tp2']:,} 減{r['rate']:+.0f}% 期待値{r['ev']:+.1f}% "
              f"決算{e.get('earn','')[5:] or '-'} 上振れ{e['P']}% {e['action']}")
        for n in r["news"][:2]:
            print(f"      {n['t'][:8]} {n['h'][:60]}")
    print(f"成績: 決済{summ['closed']}件 勝ち{summ['wins']} 平均{summ['avg']}% / 保有{summ['open']} 待機{summ['waiting']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(force="--force" in sys.argv))
