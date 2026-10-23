"""latest.json の点検と、スマホ通知の文面づくり。

  python3 tools/check.py          … データの欠け・古さの点検と、本命・次点の一覧（見立てを書く材料）
  python3 tools/check.py --push   … notes.json の見立てを読んで、通知の文面（200字以内）を1行で出す
"""
import json
import os
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")
JST = timezone(timedelta(hours=9))
VIEWS = ("買い候補", "慎重", "見送り")


def load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return json.load(f)


try:
    import jpholiday
except ImportError:
    jpholiday = None


def is_bday(d):
    if d.weekday() >= 5 or (d.month, d.day) in ((12, 31), (1, 1), (1, 2), (1, 3)):
        return False
    return not (jpholiday and jpholiday.is_holiday(d))


def bdays_between(a, b):
    """a から b までの営業日の数（a < b、a の日は数えない）。"""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if is_bday(d):
            n += 1
    return n


def picks(d):
    return [s for s in d["stocks"] if s["grade"] in ("本命", "次点")]


def warnings(d):
    m, k = d["meta"], d["market"]
    today = datetime.now(JST).date()
    out = []
    md = datetime.strptime(m["margin_date"], "%Y%m%d").date()
    if bdays_between(md, today) > 4:
        out.append(f"信用残の日付が古い（{md:%m/%d}申込分）")
    pd_ = datetime.strptime(m["price_asof"][:10], "%Y-%m-%d").date()
    if bdays_between(pd_, today) > 1:
        out.append(f"株価の日付が古い（{pd_:%m/%d}）")
    ps = picks(d)
    if not ps:
        out.append("本命・次点が0銘柄")
        return out
    near = [s for s in ps if (s.get("earnv") or {}).get("earn")]
    no_cons = [s for s in near if (s["earnv"].get("cons_gap") is None and s["earnv"].get("target_up") is None)]
    if near and len(no_cons) * 2 > len(near):
        out.append(f"市場予想（株予報）が{len(no_cons)}/{len(near)}銘柄で取れていない")
    no_fin = [s for s in near if s["earnv"].get("prog") is None and s["earnv"].get("prev_yoy") is None]
    if near and len(no_fin) * 2 > len(near):
        out.append(f"業績（株探）が{len(no_fin)}/{len(near)}銘柄で取れていない")
    out += list(m.get("warn") or [])
    if m.get("excluded"):
        out.append("異常な日の信用残を除外: " + ", ".join(m["excluded"]))
    if k["n225"] < k["stop_line"]:
        out.append("日経平均が新規買い停止ラインを下回っている")
    return out


def flags(s):
    """見立ての基準（CLAUDE.md）に機械的に当たる点。最終判断は見立てを書く側が行う。"""
    e, out = s.get("earnv") or {}, []
    bd, hold, P, tu = e.get("bdEarn"), e.get("hold"), e.get("P"), e.get("target_up")
    if bd is not None and bd <= 10 and hold is not None and hold < 1:
        out.append(f"決算まで{bd}営業日で、決算前に売る判定（値幅を取れる期間が短い）")
    if bd is not None and P is not None and P <= 45:
        out.append(f"決算で上がる確率{P}%（45%以下）")
    if bd is not None and e.get("prog") is None and e.get("prev_yoy") is None:
        out.append("業績データが取れていない")
    if tu is not None and tu >= 60:
        out.append(f"目標株価まで+{tu:.0f}%は大きすぎる（株式分割前の古い目標株価などデータの誤りの可能性）")
    elif tu is not None and tu < 0:
        out.append(f"株価が目標株価を{-tu:.0f}%上回っている")
    if s["ev"] < 0.8 and e.get("gap") is not None and e["gap"] <= -10:
        out.append("進捗が前年同期より遅れ、期待値も小さい")
    if s["val20"] < 5:
        out.append(f"売買代金が1日{s['val20']:.1f}億円と少ない（買い候補なら株数控えめ）")
    return out


def report(d):
    m, k = d["meta"], d["market"]
    print(f"更新: {m['updated']}（{m['slot']}） 信用残 {m['margin_date']} 株価 {m['price_asof']}")
    print(f"日経平均 {k['n225']:,.0f}（{k.get('n225_asof', '?')}）  停止ライン {k['stop_line']:,.0f}（余裕 {(k['n225'] / k['stop_line'] - 1) * 100:+.1f}%）")
    w = warnings(d)
    print("点検: " + ("問題なし" if not w else " / ".join(w)))
    print()
    for s in picks(d):
        L, e = s["lv"], s.get("earnv") or {}
        earn = f"決算{e['earn'][5:]} 上がる確率{e['P']}% {e['action']}" if e.get("earn") else "決算は保有期間外か未発表"
        print(f"[{s['grade']}/{'需給改善' if s['group'] == 'main' else '投げ一巡'}] {s['code']} {s['name']} "
              f"現{s['price']:,.0f} 買{L['entry']:,} 損{L['stop']:,} 利{L['tp1']:,}/{L['tp2']:,} 期待値{s['ev']:+.1f}% "
              f"RSI{s['rsi']:.0f} 25日線乖離{s['dev25']:+.1f}% 売買代金{s['val20']:.1f}億 "
              f"ATR{s['atr']:,.0f}（現在値は買い目安の{(s['price'] - L['entry']) / s['atr']:+.1f}ATR）")
        print("   " + earn)
        fl = flags(s)
        if fl:
            print("   基準に当たる点: " + " / ".join(fl))
        for f in e.get("factors") or []:
            print(f"   {'＋' if f['s'] > 0 else '－' if f['s'] < 0 else '・'}{f['t']}")
        for n in (s.get("news") or [])[:4]:
            print(f"   ニュース {n['t']} {n['h']}")
    print()
    print("候補の銘柄コード: " + " ".join(s["code"] for s in picks(d)))


def short(name, n=6):
    s = unicodedata.normalize("NFKC", name).replace("ホールディングス", "HD").replace("株式会社", "")
    return s[:n]


def push(d, notes):
    m = d["meta"]
    if not notes or str(notes.get("updated", ""))[:16] != m["updated"][:16]:
        print("ERROR: notes.json の updated が latest.json と一致しません。先に notes.json を書いてください。", file=sys.stderr)
        sys.exit(1)
    views = notes.get("views") or {}
    ps = picks(d)
    bad = [s["code"] for s in ps if views.get(s["code"]) not in VIEWS]
    if bad:
        print("ERROR: views に見立てが無い、または値が不正な銘柄: " + " ".join(bad), file=sys.stderr)
        sys.exit(1)
    when = datetime.strptime(m["updated"], "%Y-%m-%d %H:%M")
    k = d["market"]
    w = warnings(d)
    head = (("【要確認】" if any("取れていない" in x or "古い" in x for x in w) else "")
            + ("【新規買い停止中】" if k["n225"] < k["stop_line"] else "") + f"株予想 {when:%m/%d %H:%M}")
    buy = sorted([s for s in ps if views[s["code"]] == "買い候補"], key=lambda s: (s["grade"] != "本命", -s["ev"]))
    items = [f"{s['code']} {short(s['name'])} 買{s['lv']['entry']} 損{s['lv']['stop']} 利{s['lv']['tp1']}" for s in buy]
    cnt = {v: sum(1 for s in ps if views[s["code"]] == v) for v in VIEWS}
    tail = f"慎重{cnt['慎重']}・見送り{cnt['見送り']}｜日経 停止線まで{(k['n225'] / k['stop_line'] - 1) * 100:+.1f}%"
    lab = "回復待ちの候補" if k["n225"] < k["stop_line"] else "買い候補"
    for n in range(min(3, len(items)), -1, -1):
        body = (f"{lab}: " + "／".join(items[:n]) + (f" ほか{len(items) - n}" if len(items) > n else "")) if n else (
            f"{lab}なし" if not items else f"{lab}{len(items)}銘柄")
        msg = f"{head} {body}｜{tail}"
        if len(msg) <= 190:
            print(msg)
            return
    print(f"{head} 買い候補{len(items)}｜{tail}"[:190])


if __name__ == "__main__":
    latest = load("latest.json")
    if "--push" in sys.argv:
        try:
            nt = load("notes.json")
        except (OSError, ValueError):
            nt = None
        push(latest, nt)
    else:
        report(latest)
