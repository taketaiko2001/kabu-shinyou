"""市場全体の流れ（海外市場・為替・先物・株探の市況ニュース）を表示する。毎回の更新で Claude が読み、
notes.json の market と仮想売買の判断・メモに使う（数字はこの出力をそのまま使う）。

  python3 tools/market.py
"""
import os
import re
import sys
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trade as T  # noqa: E402

SYMS = [("^GSPC", "米S&P500"), ("^IXIC", "米ナスダック"), ("^SOX", "米半導体株指数SOX"), ("^DJI", "米ダウ"),
        ("^VIX", "米VIX（恐怖指数）"), ("^TNX", "米10年金利"), ("JPY=X", "ドル円"), ("^N225", "日経平均")]
PICK = ("クロージング", "前引け", "寄り付き", "NY", "米国株", "相場展望", "後場", "東京株式")


def overseas():
    import pandas as pd
    import yfinance as yf
    print("海外市場・為替（終値ベース。日付は各市場の現地の日付）:")
    for s, n in SYMS:
        try:
            d = None
            if s == "^N225":                 # Yahoo だけだと古い値が出ることがある → run.py と同じく株探の直近値で補う
                import sources as S
                d = S.index_n225()
            if d is None:
                d = yf.download(s, period="10d", interval="1d", progress=False, auto_adjust=False, threads=False)
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)
            c = d.Close.dropna()
            c = c[c > 0]
            if len(c) < 2:
                raise ValueError("行が足りない")
            if s == "^TNX":                  # 金利は変化率でなく差（%ポイント）で見る
                ch = f"前日比 {c.iloc[-1] - c.iloc[-2]:+.2f}pt" + (f"・5日 {c.iloc[-1] - c.iloc[-6]:+.2f}pt" if len(c) >= 6 else "")
            else:
                ch = f"前日比 {(c.iloc[-1] / c.iloc[-2] - 1) * 100:+.2f}%" + (f"・5日 {(c.iloc[-1] / c.iloc[-6] - 1) * 100:+.2f}%" if len(c) >= 6 else "")
            print(f"  {n:<14} {c.index[-1]:%m/%d} {c.iloc[-1]:>12,.2f}  {ch}")
        except Exception as e:
            print(f"  {n:<14} 取れない（{e}）")
    t = T.now()
    try:
        import auto as A
        fg = A.fut_gap(t)
    except Exception:
        fg = None
    if fg:
        print(f"  日経平均先物（CME・円建て）は前営業日15:30から {fg[0] * 100:+.2f}%（{fg[1]:%m/%d %H:%M}時点）"
              "。寄り付き前なら市場全体の窓の目安（実際の窓は先物の動きの6割ほどのことが多い）")
    else:
        print("  日経平均先物：取れない")


def headlines(hours=20, n=14):
    import sources as S
    from bs4 import BeautifulSoup
    lim = T.now() - timedelta(hours=hours)
    seen, rows = set(), []
    for cat, lab in ((1, "市況"), (3, "業績修正"), (5, "特集")):
        h = S.get(f"https://kabutan.jp/news/marketnews/?category={cat}")
        if not h:
            print(f"株探の{lab}ニュース：取れない")
            continue
        for tr in BeautifulSoup(h, "html.parser").select("table.s_news_list tr"):
            td = [x.get_text(" ", strip=True) for x in tr.find_all("td")]
            a = tr.find("a")
            if len(td) < 3 or not a:
                continue
            m = re.match(r"(\d{2})/(\d{2})\s+(\d{2}):(\d{2})", td[0])
            if not m:
                continue
            now = T.now()
            mo = int(m.group(1))
            y = now.year - 1 if mo > now.month + 1 else now.year
            tm = datetime(y, mo, int(m.group(2)), int(m.group(3)), int(m.group(4)), tzinfo=T.JST)
            if tm < lim or a["href"] in seen:
                continue
            seen.add(a["href"])
            rows.append((tm, lab, td[-1], a["href"]))
    rows.sort(key=lambda r: r[0], reverse=True)
    print(f"株探の市況ニュース（直近{hours}時間・新しい順）:")
    for tm, lab, title, _ in rows[:n]:
        print(f"  {tm:%m/%d %H:%M} [{lab}] {title}")
    pick = [r for r in rows if r[1] == "市況" and any(k in r[2] for k in PICK)][:2]
    for tm, lab, title, href in pick:
        h = S.get("https://kabutan.jp" + href)
        if not h:
            continue
        b = BeautifulSoup(h, "html.parser").select_one("div.body")
        if b:
            tx = re.sub(r"^.*?チャートより\s*", "", b.get_text(" ", strip=True))
            print(f"  ▼ {tm:%m/%d %H:%M} {title}\n    {tx[:420]}…")


def main():
    print(f"市場の流れ {T.now():%Y-%m-%d %H:%M}（Yahoo・株探。株価は15〜20分遅れ）")
    overseas()
    headlines()
    print("使い方：海外・先物・市況の要点を notes の market に1文で入れ、仮想売買の判断（長期の買い増し、スイングの悪材料など）にも使う。"
          "デイトレードとスイングの売買そのものは auto.py のルールどおりに行い、ニュースで足したり外したりはしない（取り消しの基準は手順7-b）。")


if __name__ == "__main__":
    main()
