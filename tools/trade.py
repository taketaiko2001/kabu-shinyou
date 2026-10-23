"""仮想売買（ペーパートレード）の記録と計算。実際の発注は一切しない。
作業用の記録は data/trade/portfolio.json（1時間ごとの確認でも書き換える）。スマホのページが読むのは
docs/data/portfolio.json で、9:00・12:30・15:30の更新で publish したときだけ書き換わる。

  python3 tools/trade.py status [--wait-close]           … 約定・損切り・引け決済・期限を処理し、口座と保有を表示して保存
                                                          （--wait-close: 今日のデイトレードの大引け値が出るまで最大12分待つ）
  python3 tools/trade.py buy  口座 コード 株数 "理由" [--stop 価格] [--tp1 価格] [--tp2 価格]
  python3 tools/trade.py sell 口座 コード 株数|all "理由"
  python3 tools/trade.py stop 口座 コード 価格 "理由"       … 損切りライン（逆指値）を変える。0 で外す
  python3 tools/trade.py cancel 注文ID "理由"               … 約定待ちの注文を取り消す
  python3 tools/trade.py memo "文"                        … ページに出すトレーダーのメモ
  python3 tools/trade.py publish                         … 作業用の記録をスマホのページ用にコピーする
  python3 tools/trade.py settle                          … 大引け後の精算（GitHub Actions 用）
機械的な売買ルール（デイトレードの朝・後場、スイング）は tools/auto.py、デイトレードの検証は tools/daylab.py。

口座は day（デイトレード）・swing（スイング）・long（長期保有）。元金は各100万円で、口座の間でお金を移さない。
約定の決め方（後から実際の値動きで確かめるので、都合のよい値は使えない）:
  - 成行注文は、注文した時刻より後に始まる最初の1分足の始値で約定する。Yahooの株価は約15〜20分遅れのため、
    約定値はたいてい次の処理（次の更新か、大引け後の精算）で確定する。
  - 損切りは逆指値（成行）：1分足の安値がラインに触れたら、その足の始値とラインの低いほうで約定。
  - デイトレードは買った日の大引け（終値）で自動決済。スイングは買った日の1か月後（休場日ならその前の営業日）の大引けで自動決済。
  - 100株単位・現物のみ。手数料・税金は0円として計算する。
"""
import calendar
import json
import math
import os
import re
import sys
import warnings
from datetime import datetime, timedelta, timezone

import pandas as pd

warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PF = os.path.join(ROOT, "data", "trade", "portfolio.json")        # 作業用
PUB = os.path.join(ROOT, "docs", "data", "portfolio.json")        # スマホのページ用（publish で更新）
LATEST = os.path.join(ROOT, "docs", "data", "latest.json")
JST = timezone(timedelta(hours=9))
CAPITAL = 1_000_000
ACCTS = {"day": "デイトレード", "swing": "スイング", "long": "長期保有"}
CAP = {"day": 5, "swing": 5, "long": 5}     # 同時に持つ銘柄数の上限（口座ごと。市原さんの決定 2026-10-23）
BUF = 1.05                                  # 約定前の成行買いは、見積もり×1.05の現金を確保しておく
DAY_STOP = 0.03                             # デイトレードの損切り（指定がないとき、買値−3%）
TFMT = "%Y-%m-%d %H:%M:%S"

# デイトレードの対象（売買代金の多い大型・主力株）。検証（2026/7/27〜10/22）も同じ銘柄で行った
DAY_LIST = dict(x.split(":") for x in """1332:ニッスイ 1605:INPEX 1801:大成建 1802:大林組 1925:大和ハウス 1928:積水ハウス 2502:アサヒ
2802:味の素 2914:JT 3382:セブン＆アイ 3407:旭化成 4063:信越化 4188:三菱ケミG 4452:花王 4502:武田 4503:アステラス 4519:中外薬
4523:エーザイ 4543:テルモ 4568:第一三共 4578:大塚HD 4661:OLC 4689:LINEヤフー 4901:富士フイルム 5020:ENEOS 5108:ブリヂストン
5401:日本製鉄 5411:JFE 5713:住友鉱 5802:住友電工 6098:リクルート 6178:日本郵政 6273:SMC 6301:コマツ 6367:ダイキン 6501:日立
6503:三菱電機 6526:ソシオネクスト 6594:ニデック 6701:NEC 6702:富士通 6723:ルネサス 6752:パナソニック 6758:ソニーG 6762:TDK
6857:アドテスト 6861:キーエンス 6902:デンソー 6920:レーザーテク 6954:ファナック 6971:京セラ 6981:村田製 7011:三菱重工
7012:川崎重工 7013:IHI 7201:日産自 7203:トヨタ 7267:ホンダ 7270:SUBARU 7741:HOYA 7751:キヤノン 7974:任天堂 8001:伊藤忠
8002:丸紅 8031:三井物産 8035:東エレク 8053:住友商事 8058:三菱商事 8306:三菱UFJ 8308:りそな 8316:三井住友FG 8411:みずほ
8591:オリックス 8604:野村 8630:SOMPO 8725:MS&AD 8750:第一生命 8766:東京海上 8801:三井不 8802:三菱地所 9020:JR東日本
9022:JR東海 9101:郵船 9104:商船三井 9107:川崎汽 9201:JAL 9202:ANA 9432:NTT 9433:KDDI 9434:ソフトバンク 9983:ファストリ
9984:SBG 6146:ディスコ 4385:メルカリ 3659:ネクソン 6532:ベイカレント 7735:SCREEN 5803:フジクラ 285A:キオクシア 6963:ローム
3436:SUMCO 4062:イビデン 6976:太陽誘電""".split())


# ---------------------------------------------------------------- 時刻・営業日
def now():
    return datetime.now(JST)


def ts(s):
    return datetime.strptime(s, TFMT).replace(tzinfo=JST)


def tstr(t):
    return pd.Timestamp(t).tz_convert(JST).strftime(TFMT) if getattr(t, "tzinfo", None) else t.strftime(TFMT)


def is_trading(d):
    if d.weekday() >= 5 or (d.month, d.day) in ((12, 31), (1, 1), (1, 2), (1, 3)):
        return False
    try:
        import jpholiday
        return not jpholiday.is_holiday(d)
    except ImportError:
        return True


def trading_on_or_before(d):
    while not is_trading(d):
        d -= timedelta(days=1)
    return d


def add_month(d):
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def bdays_until(a, b):
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        n += is_trading(d)
    return n


def close_is_final(d, t):
    """d の日の終値が確定しているか（大引け15:30の後、Yahooの反映待ちで15:45以降）。"""
    return d < t.date() or (d == t.date() and (t.hour, t.minute) >= (15, 45))


def tick(p):
    return 1 if p <= 3000 else 5 if p <= 30000 else 10


# ---------------------------------------------------------------- 株価
def _yf(codes, **kw):
    import tempfile
    import yfinance as yf
    try:
        yf.set_tz_cache_location(tempfile.mkdtemp())
    except Exception:
        pass
    out = {}

    def grab(cs, threads):
        tk = [c + ".T" for c in cs]
        try:
            d = yf.download(tk, progress=False, group_by="ticker", threads=threads, auto_adjust=False, timeout=20, **kw)
        except Exception:
            return
        if d is None or d.empty:
            return
        for c, t in zip(cs, tk):
            try:
                x = d[t] if isinstance(d.columns, pd.MultiIndex) else d
                x = x.dropna(subset=["Open", "Close"])
                if len(x):
                    out[c] = x
            except Exception:
                pass

    grab(codes, True)
    miss = [c for c in codes if c not in out]
    if miss:                      # 並列取得の取りこぼし（キャッシュの競合など）は順番に取り直す
        grab(miss, False)
    return out


def bars(codes):
    """{code: 1分足（7日分）。それより前は5分足で補う}。時刻は日本時間。"""
    m1, m5 = _yf(codes, period="7d", interval="1m"), _yf(codes, period="60d", interval="5m")
    out = {}
    for c in codes:
        a, b = m1.get(c), m5.get(c)
        for x in (a, b):
            if x is not None:
                x.index = pd.DatetimeIndex(x.index).tz_convert(JST)
        if a is not None and b is not None:
            x = pd.concat([b[b.index < a.index[0]], a])
        else:
            x = a if a is not None else b
        if x is not None:
            out[c] = x[x.Volume > 0]
    return out


def daily(codes):
    out = _yf(codes, period="3mo", interval="1d")
    for x in out.values():
        x.index = pd.DatetimeIndex(x.index).tz_localize(None).normalize()
    return out


def first_bar(x, t):
    """時刻 t より後に始まる最初の足（なければ None）。"""
    if x is None:
        return None
    t0 = pd.Timestamp(t).ceil("min")
    s = x[x.index >= t0]
    return s.iloc[0] if len(s) else None


_names = {}


def name_of(code):
    if code in DAY_LIST:
        return DAY_LIST[code]
    if code in _names:
        return _names[code]
    try:
        with open(LATEST, encoding="utf-8") as f:
            for s in json.load(f)["stocks"]:
                if s["code"] == code:
                    _names[code] = s["name"]
                    return s["name"]
    except Exception:
        pass
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import sources as S
        h = S.get(f"https://kabutan.jp/stock/?code={code}") or ""
        m = re.search(r"<title>(.+?)（", h)
        _names[code] = m.group(1) if m else code
    except Exception:
        _names[code] = code
    return _names[code]


# ---------------------------------------------------------------- 記録
def new_pf():
    t = now()
    return {"start": t.strftime("%Y-%m-%d"), "capital": CAPITAL, "updated": "", "price_asof": "", "memo": None,
            "accounts": {a: {"name": n, "cash": CAPITAL} for a, n in ACCTS.items()},
            "positions": [], "orders": [], "trades": [], "equity": [], "seq": 0}


def load():
    for f in (PF, PUB):               # 作業用が無いとき（初回）はページ用から始める
        if os.path.exists(f):
            with open(f, encoding="utf-8") as fh:
                return json.load(fh)
    return new_pf()


def save(pf):
    os.makedirs(os.path.dirname(PF), exist_ok=True)
    with open(PF, "w", encoding="utf-8") as f:
        json.dump(pf, f, ensure_ascii=False, indent=1)


def publish(pf):
    q = dict(pf, published=now().strftime("%Y-%m-%d %H:%M"))
    with open(PUB, "w", encoding="utf-8") as f:
        json.dump(q, f, ensure_ascii=False, indent=1)
    return q["published"]


def nid(pf, k):
    pf["seq"] += 1
    return f"{k}{pf['seq']}"


def log(pf, **kw):
    kw.setdefault("t", now().strftime(TFMT))
    pf["trades"].append(kw)


def reserved(pf, acct, skip=None):
    return sum(o["est"] * o["shares"] * BUF for o in pf["orders"] if o["acct"] == acct and o["side"] == "buy" and o["id"] != skip)


def fill_buy(pf, o, px, t):
    a = pf["accounts"][o["acct"]]
    sh = o["shares"]
    if px * sh > a["cash"]:
        sh = int(a["cash"] // (px * 100)) * 100
    pf["orders"].remove(o)
    if sh <= 0:
        log(pf, kind="cancel", acct=o["acct"], code=o["code"], name=o["name"], t=tstr(t),
            reason=f"約定値{px:,.1f}円では現金が足りず取り消し（{o['reason']}）")
        return
    a["cash"] = round(a["cash"] - px * sh, 1)
    p = next((p for p in pf["positions"] if p["acct"] == o["acct"] and p["code"] == o["code"]), None)
    stop = o.get("stop")
    if o["acct"] == "day" and not stop:
        stop = math.floor(px * (1 - (o.get("stop_pct") or DAY_STOP)) / tick(px)) * tick(px)
    if p:
        p["avg"] = round((p["avg"] * p["shares"] + px * sh) / (p["shares"] + sh), 2)
        p["shares"] += sh
        if stop:
            p["stop"] = stop
    else:
        d = pd.Timestamp(t).date()
        p = dict(id=nid(pf, "P"), acct=o["acct"], code=o["code"], name=o["name"], shares=sh, avg=px, opened=tstr(t),
                 stop=stop, tp1=o.get("tp1"), tp2=o.get("tp2"), rule=o.get("rule"), earn=o.get("earn"), hold=o.get("hold"),
                 reason=o["reason"], checked=tstr(pd.Timestamp(t) - pd.Timedelta(seconds=1)),
                 deadline=(d.strftime("%Y-%m-%d") if o["acct"] == "day" else
                           trading_on_or_before(add_month(d)).strftime("%Y-%m-%d") if o["acct"] == "swing" else None))
        pf["positions"].append(p)
    log(pf, kind="buy", acct=o["acct"], code=o["code"], name=o["name"], shares=sh, price=px, t=tstr(t),
        order_t=o["time"], reason=o["reason"], rule=o.get("rule"))


KIND = {"sell": "成行で売り", "stop": "逆指値で損切り", "close_day": "大引けで決済（デイトレード）",
        "close_swing": "大引けで決済（1か月の期限）", "tp1": "利確1に到達して成行で売り", "tp2": "利確2に到達して成行で売り",
        "time": "期間切れで成行で売り", "earn": "決算の前に成行で売り"}


def fill_sell(pf, p, sh, px, t, kind, o=None):
    a = pf["accounts"][p["acct"]]
    a["cash"] = round(a["cash"] + px * sh, 1)
    pnl = (px - p["avg"]) * sh
    reason = o["reason"] if o else {"stop": f"損切りライン{p['stop']:,}円に到達",
                                     "close_day": "その日のうちに手じまうルール",
                                     "close_swing": "保有期間の上限（1か月）"}.get(kind, "")
    if o and o.get("how"):
        kind = o["how"]
    log(pf, kind="sell", how=KIND[kind], acct=p["acct"], code=p["code"], name=p["name"], shares=sh, price=px,
        avg=p["avg"], pnl=round(pnl), pnl_pct=round((px / p["avg"] - 1) * 100, 2), t=tstr(t),
        order_t=o["time"] if o else None, reason=reason, opened=p["opened"], rule=p.get("rule"))
    p["shares"] -= sh
    if o:
        pf["orders"].remove(o)
    if p["shares"] <= 0:
        pf["positions"].remove(p)
        for q in [q for q in pf["orders"] if q["side"] == "sell" and q["acct"] == p["acct"] and q["code"] == p["code"]]:
            pf["orders"].remove(q)


def process(pf, t=None):
    """約定待ちの注文・損切り・引け決済・期限を、実際の値動きで時刻順に処理する。"""
    t = t or now()
    codes = sorted({p["code"] for p in pf["positions"]} | {o["code"] for o in pf["orders"]})
    if not codes:
        return {}, {}
    B, D = bars(codes), daily(codes)
    # 1. 買い注文の約定
    for o in sorted([o for o in pf["orders"] if o["side"] == "buy"], key=lambda o: o["time"]):
        b = first_bar(B.get(o["code"]), ts(o["time"]))
        od = ts(o["time"]).date()
        if b is not None and (o["acct"] != "day" or b.name.date() == od):
            fill_buy(pf, o, float(b.Open), b.name)
        elif o["acct"] == "day" and (b is not None or (close_is_final(od, t) and D.get(o["code"]) is not None
                                                        and pd.Timestamp(od) in D[o["code"]].index)):
            pf["orders"].remove(o)
            log(pf, kind="cancel", acct=o["acct"], code=o["code"], name=o["name"], reason="注文した日のうちに約定しなかったため取り消し")
    # 2. 保有ごとの出来事（損切り・売り注文・引け決済）を早い順に
    for p in list(pf["positions"]):
        x, dd = B.get(p["code"]), D.get(p["code"])
        cd = datetime.strptime(p["deadline"], "%Y-%m-%d").date() if p.get("deadline") else None
        lim = pd.Timestamp(datetime(cd.year, cd.month, cd.day, 15, 30), tz=JST) if cd else None
        if x is not None and lim is not None:
            x = x[x.index < lim]          # 期限の大引けより後の値動きでは判定しない
        while p in pf["positions"]:
            ev = []
            if p.get("stop") and x is not None:
                s = x[x.index > ts(p["checked"])]
                hit = s[s.Low <= p["stop"]]
                if len(hit):
                    ev.append((hit.index[0], min(float(hit.iloc[0].Open), float(p["stop"])), "stop", None))
            for o in [o for o in pf["orders"] if o["side"] == "sell" and o["acct"] == p["acct"] and o["code"] == p["code"]]:
                b = first_bar(x, ts(o["time"]))
                if b is not None:
                    ev.append((b.name, float(b.Open), "sell", o))
            if cd and close_is_final(cd, t):
                px = None
                if dd is not None and pd.Timestamp(cd) in dd.index:
                    px = float(dd.Close[pd.Timestamp(cd)])
                elif cd < t.date() and x is not None and (x.index.date == cd).any():
                    px = float(x[x.index.date == cd].Close.iloc[-1])   # 日足が抜けたときは、その日の最後の1分足
                if px is not None:
                    ev.append((lim, px, "close_" + p["acct"], None))
            if not ev:
                break
            tm, px, kind, o = min(ev, key=lambda e: e[0])
            sh = min(o["shares"], p["shares"]) if o else p["shares"]
            fill_sell(pf, p, sh, round(px, 1), tm, kind, o)
            if p in pf["positions"]:
                p["checked"] = tstr(tm)
        if p in pf["positions"] and x is not None and len(x):
            p["checked"] = tstr(x.index[-1])
    return B, D


def value(pf, B, D, t=None):
    t = t or now()
    asof = None
    for p in pf["positions"]:
        x, dd = B.get(p["code"]), D.get(p["code"])
        last, lt = None, None
        if x is not None and len(x):
            last, lt = float(x.Close.iloc[-1]), x.index[-1]
        if dd is not None and len(dd) and close_is_final(dd.index[-1].date(), t) and (lt is None or dd.index[-1].date() >= lt.date()):
            last, lt = float(dd.Close.iloc[-1]), pd.Timestamp(datetime.combine(dd.index[-1].date(), datetime.min.time()).replace(hour=15, minute=30), tz=JST)
        if last is None:
            last, lt = p.get("last", p["avg"]), None
        p["last"], p["last_t"] = round(last, 1), (tstr(lt) if lt is not None else p.get("last_t"))
        if lt is not None and (asof is None or lt > asof):
            asof = lt
    tot = {}
    for a, acc in pf["accounts"].items():
        mv = sum(p["last"] * p["shares"] for p in pf["positions"] if p["acct"] == a)
        acc["value"] = round(mv)
        acc["equity"] = round(acc["cash"] + mv)
        acc["pnl"] = acc["equity"] - CAPITAL
        acc["realized"] = sum(x.get("pnl", 0) for x in pf["trades"] if x["kind"] == "sell" and x["acct"] == a)
        acc["n"] = sum(1 for p in pf["positions"] if p["acct"] == a)
        tot[a] = acc["equity"]
    pf["updated"] = t.strftime("%Y-%m-%d %H:%M")
    if asof is not None:
        pf["price_asof"] = tstr(asof)[:16]
    e = pf["equity"]
    row = dict(t=pf["updated"], **tot)
    if e and e[-1]["t"][:10] == row["t"][:10]:
        e[-1] = row            # 1日1点（その日の最後の値）
    else:
        e.append(row)
    del e[:-400]


# ---------------------------------------------------------------- 表示
def yen(v):
    return f"{v:+,.0f}円" if v else "±0円"


def earnings(pf):
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import sources as S
    except Exception:
        return
    for p in pf["positions"]:
        if p["acct"] == "day":
            continue
        try:
            e = S.kabutan_top(p["code"]).get("earn", "")
            if e:
                p["earn"] = e.replace("/", "-")
        except Exception:
            pass


def show(pf):
    print(f"仮想売買 {pf['updated']}（株価 {pf['price_asof'] or '—'} 時点・Yahooは約15〜20分遅れ）")
    tot = 0
    for a, acc in pf["accounts"].items():
        tot += acc["equity"]
        print(f"  {acc['name']:<6} 評価額 {acc['equity']:>11,}円  損益 {yen(acc['pnl']):>10}（{acc['pnl'] / CAPITAL * 100:+.2f}%）"
              f"  現金 {acc['cash']:>11,.0f}円  保有 {acc['n']}/{CAP[a]}銘柄  約定待ちで確保 {reserved(pf, a):,.0f}円")
    print(f"  合計     評価額 {tot:>11,}円  損益 {yen(tot - 3 * CAPITAL)}（{(tot / (3 * CAPITAL) - 1) * 100:+.2f}%）")
    t = now()
    if pf["positions"]:
        print("保有:")
    for p in pf["positions"]:
        r = (p["last"] / p["avg"] - 1) * 100
        dl = ""
        if p["acct"] == "swing":
            d = datetime.strptime(p["deadline"], "%Y-%m-%d").date()
            dl = f" 期限 {d:%m/%d}（あと{bdays_until(t.date(), d)}営業日）"
        elif p["acct"] == "day":
            dl = " 大引けで自動決済"
        tp = "".join(f" {k}={p[k]:,.0f}" for k in ("tp1", "tp2") if p.get(k))
        if p.get("earn"):
            e = datetime.strptime(p["earn"], "%Y-%m-%d").date()
            dl += f" 決算 {e:%m/%d}（あと{bdays_until(t.date(), e)}営業日）" if e >= t.date() else ""
        print(f"  [{ACCTS[p['acct']]}] {p['code']} {p['name']} {p['shares']}株 買値 {p['avg']:,.1f} → 現在 {p['last']:,.1f}"
              f"（{r:+.2f}%・{yen((p['last'] - p['avg']) * p['shares'])}） 損切り {p['stop'] or 'なし'}{tp}{dl} 買った日時 {p['opened'][5:16]}")
        print(f"      理由: {p['reason']}")
    for o in pf["orders"]:
        print(f"  約定待ち {o['id']} [{ACCTS[o['acct']]}] {'買い' if o['side'] == 'buy' else '売り'} {o['code']} {o['name']} {o['shares']}株"
              f"（注文 {o['time'][5:16]}・見積もり {o['est']:,.1f}円）")
    rec = sorted([x for x in pf["trades"] if x["t"] >= (t - timedelta(days=3)).strftime(TFMT)], key=lambda x: x["t"])
    if rec:
        print("直近3日の記録:")
    for x in rec:
        if x["kind"] == "buy":
            print(f"  {x['t'][5:16]} [{ACCTS[x['acct']]}] 買い {x['code']} {x['name']} {x['shares']}株 @{x['price']:,.1f}")
        elif x["kind"] == "sell":
            print(f"  {x['t'][5:16]} [{ACCTS[x['acct']]}] 売り {x['code']} {x['name']} {x['shares']}株 @{x['price']:,.1f}"
                  f" {x['how']} 損益 {yen(x['pnl'])}（{x['pnl_pct']:+.2f}%）")
        else:
            print(f"  {x['t'][5:16]} [{ACCTS[x['acct']]}] {x['kind']} {x['code']} {x.get('reason', '')}")
    try:
        with open(LATEST, encoding="utf-8") as f:
            k = json.load(f)["market"]
        print(f"日経平均 {k['n225']:,.0f}（{k.get('n225_asof', '?')}）停止ライン {k['stop_line']:,.0f}（余裕 {(k['n225'] / k['stop_line'] - 1) * 100:+.1f}%）")
    except Exception:
        pass


# ---------------------------------------------------------------- 注文
class TradeError(Exception):
    pass


def err(msg):
    raise TradeError(msg)


def latest_price(code):
    x = bars([code]).get(code)
    if x is None or not len(x):
        d = daily([code]).get(code)
        return (float(d.Close.iloc[-1]), d.index[-1]) if d is not None and len(d) else (None, None)
    return float(x.Close.iloc[-1]), x.index[-1]


def cmd_buy(pf, acct, code, shares, reason, stop=None, tp1=None, tp2=None, rule=None, stop_pct=None,
            earn=None, hold=None, est=None, name=None, quiet=False):
    t = now()
    if acct not in ACCTS:
        err("口座は day / swing / long のどれか")
    if not re.fullmatch(r"\d{3}[0-9A-Z]", code):
        err("銘柄コードが不正")
    if shares <= 0 or shares % 100:
        err("株数は100株単位")
    if not reason.strip():
        err("理由を書く")
    if not is_trading(t.date()):
        err("今日は休場日")
    if acct == "day" and not ((8, 25) <= (t.hour, t.minute) < (14, 30)):
        err("デイトレードの買いは 8:25〜14:30 だけ（9:00前の注文は寄り付きで約定。その日の大引けで決済するため）")
    held = {p["code"] for p in pf["positions"] if p["acct"] == acct} | {o["code"] for o in pf["orders"] if o["acct"] == acct and o["side"] == "buy"}
    if code not in held and len(held) >= CAP[acct]:
        err(f"{ACCTS[acct]}は同時に{CAP[acct]}銘柄まで（全体で5銘柄まで）")
    if acct == "day" and any(x["kind"] == "sell" and x["acct"] == "day" and x["code"] == code and x["t"][:10] == t.strftime("%Y-%m-%d") for x in pf["trades"]):
        err("同じ日に同じ銘柄を買い直さない（現物の差金決済にあたるため）")
    et = None
    if est is None:
        est, et = latest_price(code)
    if est is None:
        err("株価が取れない")
    a = pf["accounts"][acct]
    free = a["cash"] - reserved(pf, acct)
    need = est * shares * BUF
    if need > free:
        mx = int(free // (est * BUF * 100)) * 100
        err(f"現金が足りない（必要の見積もり {need:,.0f}円 > 使える現金 {free:,.0f}円）。買えるのは {mx}株まで")
    if stop is not None and stop >= est:
        err(f"損切りライン {stop} が現在値 {est} 以上")
    o = dict(id=nid(pf, "O"), acct=acct, side="buy", code=code, name=name or name_of(code), shares=shares,
             time=t.strftime(TFMT), est=round(est, 1), stop=stop, tp1=tp1, tp2=tp2, rule=rule, stop_pct=stop_pct,
             earn=earn, hold=hold, reason=reason.strip())
    pf["orders"].append(o)
    log(pf, kind="order", acct=acct, side="buy", code=code, name=o["name"], shares=shares, reason=o["reason"], t=o["time"], rule=rule)
    if not quiet:
        print(f"注文 {o['id']}: [{ACCTS[acct]}] {code} {o['name']} {shares}株 成行買い（見積もり {est:,.1f}円"
              + (f"・{pd.Timestamp(et).strftime('%H:%M')}の値）" if et is not None else "）"))
    return o


def cmd_sell(pf, acct, code, shares, reason, how=None, quiet=False):
    t = now()
    p = next((p for p in pf["positions"] if p["acct"] == acct and p["code"] == code), None)
    if not p:
        err("その口座にその銘柄の保有がない")
    if not reason.strip():
        err("理由を書く")
    pend = sum(o["shares"] for o in pf["orders"] if o["side"] == "sell" and o["acct"] == acct and o["code"] == code)
    sh = p["shares"] - pend if shares == "all" else int(shares)
    if sh <= 0 or sh % 100 or sh + pend > p["shares"]:
        err(f"売れる株数は {p['shares'] - pend}株まで（100株単位）")
    o = dict(id=nid(pf, "O"), acct=acct, side="sell", code=code, name=p["name"], shares=sh, time=t.strftime(TFMT),
             est=p.get("last", p["avg"]), reason=reason.strip(), how=how)
    pf["orders"].append(o)
    log(pf, kind="order", acct=acct, side="sell", code=code, name=p["name"], shares=sh, reason=o["reason"], t=o["time"])
    if not quiet:
        print(f"注文 {o['id']}: [{ACCTS[acct]}] {code} {p['name']} {sh}株 成行売り")
    return o


def cmd_stop(pf, acct, code, price, reason, quiet=False):
    p = next((p for p in pf["positions"] if p["acct"] == acct and p["code"] == code), None)
    if not p:
        err("その口座にその銘柄の保有がない")
    price = float(price) or None
    if price and price >= p.get("last", p["avg"]):
        err("損切りラインが現在値以上（すぐ売りたいときは sell を使う）")
    old, p["stop"] = p.get("stop"), price
    log(pf, kind="stop", acct=acct, code=code, name=p["name"], old=old, new=price, reason=reason.strip())
    if not quiet:
        print(f"損切りライン: {code} {old} → {price}")


def cmd_cancel(pf, oid, reason):
    o = next((o for o in pf["orders"] if o["id"] == oid), None)
    if not o:
        err("その注文IDは約定待ちにない")
    pf["orders"].remove(o)
    log(pf, kind="cancel", acct=o["acct"], code=o["code"], name=o["name"], reason=reason.strip() or "取り消し")
    print(f"取り消し: {oid}")


def day_open_today(pf, t):
    """今日買ったデイトレードで、まだ決済されていないもの（大引け値の確定待ち）があるか。"""
    d = t.strftime("%Y-%m-%d")
    return any(p["acct"] == "day" and p["opened"][:10] == d for p in pf["positions"]) or         any(o["acct"] == "day" and o["time"][:10] == d for o in pf["orders"])


def run_status(pf, wait_close=False):
    import time
    B, D = process(pf)
    t = now()
    if wait_close and is_trading(t.date()) and (t.hour, t.minute) >= (15, 30):
        for _ in range(12):                    # 大引け値が Yahoo に出るまで、1分ごとに最大12分待つ
            if not day_open_today(pf, now()):
                break
            print("大引け値の反映待ち…")
            time.sleep(60)
            B, D = process(pf)
    value(pf, B, D)
    return B, D


def main(argv):
    if not argv:
        print(__doc__)
        return
    pf = load()
    c = argv[0]
    if c == "status":
        run_status(pf, "--wait-close" in argv)
        earnings(pf)
        save(pf)
        show(pf)
        return
    if c == "publish":
        print("ページ用に反映: " + publish(pf))
        return
    if c == "settle":      # 大引け後の精算（GitHub Actions から）。15:30の更新が反映できなかったときの予備
        t = now()
        if not is_trading(t.date()) or not (pf["positions"] or pf["orders"]):
            print("精算するものなし")
            return
        run_status(pf)
        save(pf)
        show(pf)
        try:
            with open(PUB, encoding="utf-8") as f:
                pub = json.load(f).get("published", "")
        except Exception:
            pub = ""
        if pub < t.strftime("%Y-%m-%d") + " 15:30":
            print("15:30の反映がまだなので、ページ用にも反映: " + publish(pf))
        return
    if c == "memo":
        pf["memo"] = {"t": now().strftime("%Y-%m-%d %H:%M"), "text": " ".join(argv[1:]).strip()}
        save(pf)
        print("メモを保存")
        return
    if c == "buy":
        opt = {}
        for k in ("--stop", "--tp1", "--tp2"):
            if k in argv:
                i = argv.index(k)
                opt[k[2:]] = float(argv[i + 1])
                argv = argv[:i] + argv[i + 2:]
        if len(argv) < 5:
            err('使い方: buy 口座 コード 株数 "理由" [--stop 価格] [--tp1 価格] [--tp2 価格]')
        cmd_buy(pf, argv[1], argv[2].upper(), int(argv[3]), " ".join(argv[4:]), **opt)
    elif c == "sell":
        if len(argv) < 5:
            err('使い方: sell 口座 コード 株数|all "理由"')
        cmd_sell(pf, argv[1], argv[2].upper(), argv[3], " ".join(argv[4:]))
    elif c == "stop":
        if len(argv) < 5:
            err('使い方: stop 口座 コード 価格 "理由"')
        cmd_stop(pf, argv[1], argv[2].upper(), argv[3], " ".join(argv[4:]))
    elif c == "cancel":
        cmd_cancel(pf, argv[1], " ".join(argv[2:]))
    else:
        err("不明なコマンド: " + c)
    B, D = process(pf)
    value(pf, B, D)
    save(pf)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except TradeError as e:
        print("ERROR: " + str(e), file=sys.stderr)
        sys.exit(1)
