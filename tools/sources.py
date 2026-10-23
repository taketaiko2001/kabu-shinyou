"""データ取得と解析（JPX・株探・株予報・Yahoo Finance・stooq）。

どの関数も、取得や解析に失敗したら例外を投げずに None / 空を返す。
呼び出し側は「取れなかった項目は使わない」前提で組み立てる。
"""
import io
import re
import time
from datetime import datetime

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
    "Accept-Language": "ja,en;q=0.8",
}
SESSION = requests.Session()
SESSION.headers.update(UA)


def get(url, binary=False, tries=3, wait=0.35, timeout=30):
    for i in range(tries):
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 200:
                time.sleep(wait)
                return r.content if binary else r.text
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1))
    return None


def num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").replace("＋", "+").replace("－", "-").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    v = float(m.group())
    if "倍" in s and "倍" == s.strip()[-1:]:
        return v
    return v


# ---------------------------------------------------------------- JPX 信用残（日々・全銘柄）
JPX_LIST = "https://www.jpx.co.jp/markets/statistics-equities/margin/01.html"
_ROW = re.compile(
    r"^([A-Z]) (.+?) ?(プライム|スタンダード|グロース|投信等) (貸|制|他) ([0-9A-Z]{5}) (JP[0-9A-Z]{10}) 株数 Shs\. (.+)$",
    re.M,
)
_COLS = ["sell", "sell_chg", "sell_ratio", "buy", "buy_chg", "buy_ratio", "sell_neg", "sell_neg_chg",
         "sell_std", "sell_std_chg", "buy_neg", "buy_neg_chg", "buy_std", "buy_std_chg"]


def jpx_margin_files():
    """公開中の日次PDFの [(日付YYYYMMDD, url)] を新しい順で返す。"""
    html = get(JPX_LIST)
    if not html:
        return []
    out = []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        m = re.search(r"(\d{8})_mtall\.pdf$", a["href"])
        if m:
            out.append((m.group(1), "https://www.jpx.co.jp" + a["href"]))
    return sorted(set(out), reverse=True)


def parse_jpx_pdf(content):
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(content)
    txt = "\n".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf))).replace("\r", "")
    rows = []
    for m in _ROW.finditer(txt):
        _, name, mkt, kind, code5, _, rest = m.groups()
        toks, vals, i = rest.split(), [], 0
        while i < len(toks):
            t = toks[i]
            if t == "▲" and i + 1 < len(toks):
                vals.append(-float(toks[i + 1].replace(",", "")))
                i += 2
                continue
            if t in ("*", "-"):
                vals.append(np.nan)
            elif t.endswith("%"):
                vals.append(float(t[:-1]))
            else:
                try:
                    vals.append(float(t.replace(",", "")))
                except ValueError:
                    vals.append(None)
            i += 1
        if len(vals) != 14 or mkt == "投信等":
            continue
        name = name.replace("普通株式", "").replace("　", " ").strip()
        if re.search("優先|種類株式|受益証券|投資証券", name):
            continue
        d = dict(code5=code5, code=code5[:4], name=name, mkt=mkt, kind=kind)
        d.update(dict(zip(_COLS, vals)))
        rows.append(d)
    df = pd.DataFrame(rows)
    for c in _COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.drop_duplicates("code5").set_index("code5")


# ---------------------------------------------------------------- 株価
def prices_yf(codes, period="2y"):
    """{code: DataFrame(Open,High,Low,Close,Volume)}。失敗した銘柄は含まない。"""
    import tempfile
    import yfinance as yf
    try:
        yf.set_tz_cache_location(tempfile.mkdtemp())
    except Exception:
        pass
    out = {}

    def grab(batch, threads):
        try:
            d = yf.download(batch, period=period, interval="1d", auto_adjust=False, progress=False,
                            group_by="ticker", threads=threads, timeout=20)
        except Exception:
            return
        if d is None or d.empty:
            return
        for t in batch:
            try:
                x = d[t].dropna(subset=["Close"]) if len(batch) > 1 or isinstance(d.columns, pd.MultiIndex) else d.dropna(subset=["Close"])
                if len(x) > 30:
                    out[t[:-2]] = x[["Open", "High", "Low", "Close", "Volume"]]
            except Exception:
                pass

    tick = [c + ".T" for c in codes]
    for i in range(0, len(tick), 200):          # 並列でまとめて取得
        grab(tick[i:i + 200], True)
    miss = [t for t in tick if t[:-2] not in out]
    for i in range(0, len(miss), 40):           # 取りこぼし（キャッシュ競合など）は順番に取り直す
        grab(miss[i:i + 40], False)
    return out


def prices_stooq(code):
    txt = get(f"https://stooq.com/q/d/l/?s={code.lower()}.jp&i=d", wait=0.3)
    if not txt or "Date" not in txt[:50]:
        return None
    x = pd.read_csv(io.StringIO(txt), parse_dates=["Date"]).set_index("Date")
    return x[["Open", "High", "Low", "Close", "Volume"]].iloc[-520:]


def prices(codes, period="2y"):
    out = prices_yf(codes, period)
    miss = [c for c in codes if c not in out]
    for c in miss[:150]:
        x = prices_stooq(c)
        if x is not None and len(x) > 30:
            out[c] = x
    return out


def kabutan_index_recent():
    """株探の日経平均の日足（直近30日ほど・場中は当日分も）。Yahoo の抜け・古さを補う。"""
    html = get("https://kabutan.jp/stock/kabuka?code=0000")
    if not html:
        return None
    rows = []
    for tr in BeautifulSoup(html, "html.parser").select("table tr"):
        cells = [c.get_text(strip=True) for c in tr.select("th,td")]
        if len(cells) >= 5 and re.match(r"^\d{2}/\d{2}/\d{2}$", cells[0]):
            o, h, l, c = (num(x) for x in cells[1:5])
            if c:
                rows.append(dict(Date=pd.Timestamp("20" + cells[0].replace("/", "-")), Open=o, High=h, Low=l, Close=c))
    if not rows:
        return None
    return pd.DataFrame(rows).drop_duplicates("Date").set_index("Date").sort_index()


def index_n225(period="2y"):
    """日経平均の日足。長い履歴は Yahoo、直近は株探で上書き・補完する（Yahoo は行の抜けや古い値が出ることがある）。"""
    import yfinance as yf
    try:
        d = yf.download("^N225", period=period, interval="1d", auto_adjust=False, progress=False, threads=False, timeout=20)
        d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
        d = d.dropna(subset=["Close"])[["Open", "High", "Low", "Close"]]
        d.index = pd.DatetimeIndex(d.index).tz_localize(None).normalize()
    except Exception:
        d = None
    if d is None or len(d) < 200:
        return None
    k = kabutan_index_recent()
    if k is not None:
        d = pd.concat([d[~d.index.isin(k.index)], k]).sort_index()
    return d


# ---------------------------------------------------------------- 株探
def kabutan_top(code):
    html = get(f"https://kabutan.jp/stock/?code={code}")
    if not html:
        return {}
    tx = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    info = {}
    m = re.search(r"PER PBR 利回り 信用倍率 (\S+) 倍 (\S+) 倍 (\S+) ％ (\S+) 倍", tx)
    if m:
        info.update(per=m.group(1), pbr=m.group(2), yld=m.group(3))
    m = re.search(r"決算発表予定日 (\d{4}/\d{2}/\d{2})", tx)
    info["earn"] = m.group(1) if m else ""
    m = re.search(r"概要 (.+?) 業種 (\S+)", tx)
    if m:
        info["desc"], info["sector"] = m.group(1)[:90], m.group(2)
    return info


def kabutan_margin_hist(code):
    html = get(f"https://kabutan.jp/stock/kabuka?code={code}&ashi=shin")
    if not html:
        return []
    out = []
    for tb in BeautifulSoup(html, "html.parser").find_all("table"):
        rows = tb.find_all("tr")
        if len(rows) > 5 and "買い残" in rows[0].get_text():
            for rr in rows[2:]:
                c = [q.get_text(strip=True) for q in rr.find_all(["td", "th"])]
                if len(c) >= 7 and re.match(r"\d{2}/\d{2}/\d{2}", c[0]):
                    out.append(dict(d=c[0], b=num(c[5]), s=num(c[3])))
    return out[::-1]  # 古い順


def kabutan_news(code, n=8):
    html = get(f"https://kabutan.jp/stock/news?code={code}")
    if not html:
        return []
    out = []
    for tr in BeautifulSoup(html, "html.parser").select("table.s_news_list tr"):
        td = tr.find_all("td")
        if len(td) >= 3:
            out.append(dict(t=td[0].get_text(" ", strip=True), h=td[2].get_text(" ", strip=True)))
    return out[:n]


_PERIOD_Y = re.compile(r"^(予\s*)?(?:[A-Z]\s+)?(\d{4})\.(\d{2})$")
_PERIOD_Q = re.compile(r"^(予\s*)?(?:[A-Z]\s+)?(\d{2})\.(\d{2})-(\d{2})$")


def _date(s):
    m = re.match(r"(\d{2})/(\d{2})/(\d{2})", s or "")
    return datetime(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def kabutan_finance(code):
    """通期（実績＋会社予想）と四半期実績を返す。単位は百万円。"""
    html = get(f"https://kabutan.jp/stock/finance?code={code}")
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    annual, quarters = [], {}
    for tb in soup.find_all("table"):
        head = tb.find("tr")
        if not head:
            continue
        ht = head.get_text(" ", strip=True)
        if "決算期" not in ht or "発表日" not in ht or "売上高" not in ht:
            continue
        cols = [c.get_text(" ", strip=True) for c in head.find_all(["th", "td"])]
        if "経常益" not in cols:
            continue
        ix = {k: cols.index(k) for k in ["売上高", "営業益", "経常益", "最終益", "発表日"] if k in cols}
        for tr in tb.find_all("tr")[1:]:
            c = [q.get_text(" ", strip=True) for q in tr.find_all(["th", "td"])]
            if len(c) < len(cols):
                continue
            lab = c[0]
            rec = {k: num(c[i]) for k, i in ix.items() if k != "発表日"}
            rec["date"] = _date(c[ix["発表日"]]) if "発表日" in ix else None
            my = _PERIOD_Y.match(lab)
            mq = _PERIOD_Q.match(lab)
            if my:
                rec.update(fy=int(my.group(2)), fm=int(my.group(3)), fc=bool(my.group(1)))
                annual.append(rec)
            elif mq and not mq.group(1):
                m1, m2 = int(mq.group(3)), int(mq.group(4))
                span = (m2 - m1) % 12 + 1
                if span == 3:
                    y = 2000 + int(mq.group(2))
                    end_y = y if m2 >= m1 else y + 1
                    rec.update(y=end_y, m=m2, label=lab)
                    quarters[(end_y, m2)] = rec
    if not annual:
        return None
    seen, ann = set(), []
    for a in annual:  # 同じ期が別表に重複して出るので最初の1件を残す
        k = (a["fy"], a["fm"], a["fc"])
        if k not in seen:
            seen.add(k)
            ann.append(a)
    q = [quarters[k] for k in sorted(quarters)]
    return dict(annual=ann, quarters=q)


# ---------------------------------------------------------------- 株予報（コンセンサス）
def kabuyoho(code):
    html = get(f"https://kabuyoho.jp/reportTop?bcode={code}")
    if not html:
        return {}
    tx = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    out = {}
    m = re.search(r"予想経常利益 \(予\) 増益率 ([\-\d,\.]+) 百万円", tx)
    if m:
        out["ord_co"] = num(m.group(1))
    m = re.search(r"予想経常利益 \(コ\) 増益率 ([\-\d,\.]+) 百万円", tx)
    if m:
        out["ord_cons"] = num(m.group(1))
    m = re.search(r"目標株価 \(コ\) 株価かい離率 ([\-\d,\.]+) 円", tx)
    if m:
        out["target"] = num(m.group(1))
    m = re.search(r"レーティング [★☆ ]+ ([\d\.]+)", tx)
    if m:
        out["rating"] = num(m.group(1))
    return out


def jpx_market_total():
    """市場全体の信用残（週次・2市場合計）。単位: 株数=千株, 金額=百万円。"""
    html = get("https://www.jpx.co.jp/markets/statistics-equities/margin/04.html")
    if not html:
        return {}
    links = sorted(set(re.findall(r'href="([^"]+/(\d{8})_mtcurrent\.xlsx)"', html)), key=lambda t: t[1])
    if not links:
        return {}
    href, date = links[-1]
    content = get("https://www.jpx.co.jp" + href, binary=True)
    if not content:
        return {}
    try:
        x = pd.read_excel(io.BytesIO(content), header=None)
        return dict(date=date, buy_shares=float(x.iloc[13, 6]), buy_chg=float(x.iloc[13, 7]),
                    buy_val=float(x.iloc[14, 6]), sell_val=float(x.iloc[14, 4]),
                    ratio=round(float(x.iloc[14, 6]) / float(x.iloc[14, 4]), 2))
    except Exception:
        return {}
