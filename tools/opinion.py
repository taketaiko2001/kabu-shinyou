"""世間の評価を集める：実際のお金の動きと、世間の声・関心。毎回の更新で run.py のあとに動かす。

  python3 tools/opinion.py

対象は、最新の予想（docs/data/latest.json）の本命・次点・監視の銘柄と、仮想売買の保有・注文の銘柄。
集めるもの（どれも公式のAPI・公開データで、自動で読んでよいもの）:
  - 機関投資家の空売り … JPX「空売り残高に関する情報」（日ごとの報告。直近約3週間分）
  - 世間の関心         … Wikipedia（日本語版）の閲覧数（Wikimedia の公式API。記事は Wikidata と Wikipedia の検索で探す）
  - 個人の声           … Bluesky の公式API（投資の言葉と一緒に書かれた投稿だけを数える。格上げ・格下げの話題も拾う）
  - 注目度             … 売買代金の直近5日と20日の比（latest.json の val5・val20）
  - 個人の信用買い     … 信用買い残の増減（latest.json。スクリーナー本体の値）
出力: docs/data/opinion.json（ページの「世間の評価」）、data/opinion/YYYYMMDD.json（後で予想に効くかを検証するための記録）。
どれも株価の予想に効くかはまだ検証していない。ひとつの情報に頼らず、見立ての材料の1つとして使う。
投稿の本文は表示用のファイルに残さない（件数だけ）。本文は Claude が読むための画面出力にだけ出す。
Bluesky は環境変数 BSKY_HANDLE と BSKY_APP_PASSWORD（アプリパスワード）があればログインして使い、なければログインなしで読む。
"""
import io
import json
import os
import re
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JST = timezone(timedelta(hours=9))
NOW = datetime.now(JST)
OUT = os.path.join(ROOT, "docs", "data", "opinion.json")
HIST = os.path.join(ROOT, "data", "opinion")
WIKI_MAP = os.path.join(ROOT, "data", "cache", "wiki_titles.json")
UA = {"User-Agent": "kabu-shinyou/0.1 (+https://github.com/taketaiko2001/kabu-shinyou; personal stock research)"}
S = requests.Session()
S.headers.update(UA)
DEAD = set()          # つながらなかったサイト（この回はもう読まない）


def fetch(url, params=None, timeout=20, wait=0.0, binary=False, headers=None, method="GET", body=None):
    host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
    if host in DEAD:
        return None
    try:
        if method == "POST":
            r = S.post(url, json=body, timeout=timeout, headers=headers)
        else:
            r = S.get(url, params=params, timeout=timeout, headers=headers)
    except requests.RequestException as e:
        print(f"  {host} に接続できない（{type(e).__name__}）。この回は使わない")
        DEAD.add(host)
        return None
    if wait:
        time.sleep(wait)
    if r.status_code in (401, 403, 407, 429):
        print(f"  {host} が {r.status_code} を返した。この回は使わない")
        DEAD.add(host)
        return None
    if r.status_code != 200:
        return None
    return r.content if binary else r


# ---------------------------------------------------------------- 対象
def targets():
    t = {}
    try:
        with open(os.path.join(ROOT, "docs", "data", "latest.json"), encoding="utf-8") as f:
            L = json.load(f)
        for s in L["stocks"]:
            t[s["code"]] = dict(name=s["name"], val5=s.get("val5"), val20=s.get("val20"), rate4=s.get("rate4"),
                                ratio1=s.get("ratio1"), pchg=s.get("pchg"))
    except (OSError, ValueError, KeyError):
        L = None
    try:
        with open(os.path.join(ROOT, "data", "trade", "portfolio.json"), encoding="utf-8") as f:
            pf = json.load(f)
        for p in pf.get("positions", []) + pf.get("orders", []):
            if p["acct"] != "day":
                t.setdefault(p["code"], dict(name=p["name"]))
    except (OSError, ValueError, KeyError):
        pass
    return t


# ---------------------------------------------------------------- 機関の空売り（JPX）
def shorts(codes, files=15):
    import pandas as pd
    base = "https://www.jpx.co.jp"
    r = fetch(base + "/markets/public/short-selling/index.html")
    if r is None:
        return {}
    links = re.findall(r'href="([^"]+/(\d{8})_Short_Positions\.xls)"', r.text)[:files]
    rows = []
    for href, d in links:
        b = fetch(base + href, binary=True, wait=0.5, timeout=40)
        if not b:
            continue
        try:
            x = pd.read_excel(io.BytesIO(b), header=None)
        except Exception:
            continue
        x = x.iloc[8:, [1, 2, 5, 10, 13, 14]]
        x.columns = ["calc", "code", "seller", "ratio", "pcalc", "pratio"]
        x["code"] = x["code"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
        x = x[x["code"].isin(codes)]
        x["pub"] = d
        rows.append(x)
    if not rows:
        return {}
    a = pd.concat(rows)
    for c in ("ratio", "pratio"):
        a[c] = pd.to_numeric(a[c], errors="coerce")
    a["calc"] = pd.to_datetime(a["calc"], errors="coerce")
    out = {"_files": len(links), "_from": min(d for _, d in links), "_to": max(d for _, d in links)}
    for c, g in a.groupby("code"):
        last = g.sort_values("calc").groupby("seller").tail(1)            # 機関ごとの最新の報告
        held = last[last.ratio >= 0.005]
        out[c] = dict(n=int(len(g)), up=int((g.ratio > g.pratio).sum()), down=int((g.ratio < g.pratio).sum()),
                      sellers=int(len(held)), total=round(float(held.ratio.sum()) * 100, 2),
                      last=f"{g.calc.max():%m/%d}")
    return out


# ---------------------------------------------------------------- 世間の関心（Wikipedia）
def _load_map():
    try:
        with open(WIKI_MAP, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def wiki_titles(t):
    """証券コード → 日本語版 Wikipedia の記事名。Wikidata（月1回まとめて）→ 見つからなければ Wikipedia の検索。"""
    m = _load_map()
    today = NOW.strftime("%Y-%m-%d")
    meta = m.get("_wikidata", "")
    if not meta or meta < (NOW - timedelta(days=30)).strftime("%Y-%m-%d"):
        q = ("SELECT ?code ?title WHERE { ?item p:P414 ?st . ?st ps:P414 wd:Q217475 ; pq:P249 ?code . "
             "?art schema:about ?item ; schema:isPartOf <https://ja.wikipedia.org/> ; schema:name ?title . }")
        r = fetch("https://query.wikidata.org/sparql", params={"query": q, "format": "json"}, timeout=90,
                  headers={"Accept": "application/sparql-results+json"})
        if r is not None:
            for b in r.json()["results"]["bindings"]:
                c = b["code"]["value"]
                if c not in m or not m[c].get("title"):
                    m[c] = {"title": b["title"]["value"], "t": today, "by": "wikidata"}
            m["_wikidata"] = today
    for c, v in t.items():
        e = m.get(c)
        if e and (e.get("title") or e.get("t", "") >= (NOW - timedelta(days=30)).strftime("%Y-%m-%d")):
            continue
        head = re.split(r"[\s　]", v["name"])[0]
        r = fetch("https://ja.wikipedia.org/w/api.php", wait=1.0,
                  params={"action": "query", "list": "search", "srsearch": f'insource:"{c}" {head}', "srlimit": 5, "format": "json"})
        title = ""
        if r is not None:
            for h in r.json().get("query", {}).get("search", []):
                ti = h["title"]
                stem = re.sub(r"\s*[（(].*?[)）]\s*$", "", ti)
                if ti.startswith("日本の企業一覧") or "指数" in ti:
                    continue
                if stem and (stem in v["name"] or head in stem or stem.replace(" ", "") in v["name"].replace(" ", "")):
                    title = ti
                    break
        m[c] = {"title": title, "t": today, "by": "search"}
    os.makedirs(os.path.dirname(WIKI_MAP), exist_ok=True)
    with open(WIKI_MAP, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=0, sort_keys=True)
    return {c: (m.get(c) or {}).get("title", "") for c in t}


def pageviews(title):
    from urllib.parse import quote
    a, b = (NOW - timedelta(days=62)).strftime("%Y%m%d"), (NOW - timedelta(days=1)).strftime("%Y%m%d")
    r = fetch(f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/ja.wikipedia/all-access/user/"
              f"{quote(title.replace(' ', '_'), safe='')}/daily/{a}/{b}", wait=0.2)
    if r is None:
        return None
    v = [x["views"] for x in r.json().get("items", [])]
    if len(v) < 21:
        return None
    a7, a28 = sum(v[-7:]) / 7, sum(v[-35:-7]) / max(1, len(v[-35:-7]))
    return dict(avg7=round(a7), avg28=round(a28), ratio=round(a7 / a28, 2) if a28 >= 1 else None, max7=max(v[-7:]))


# ---------------------------------------------------------------- 個人の声（Bluesky）
INVEST = re.compile(r"株価|株式(?!会社)|銘柄|決算|業績|目標株価|格上げ|格下げ|レーティング|投資判断|上方修正|下方修正|増益|減益|"
                    r"最高益|ストップ高|ストップ安|急騰|急落|ＰＴＳ|PTS|信用買|空売り|配当|増配|減配|自社株買|日本株|株主|"
                    r"含み益|含み損|買い増|損切|利確|ナンピン|寄り付|大引け")
POS = re.compile(r"上方修正|増益|最高益|格上げ|引き上げ|ストップ高|急騰|好決算|増配|自社株買|上振れ|反発|買い増")
NEG = re.compile(r"下方修正|減益|赤字|格下げ|引き下げ|ストップ安|急落|減配|下振れ|不祥事|続落|損切")
RUP = re.compile(r"格上げ|(目標株価|レーティング|投資判断)[^。\n]{0,40}引き上げ")
RDN = re.compile(r"格下げ|(目標株価|レーティング|投資判断)[^。\n]{0,40}引き下げ")


def bsky_base():
    h, pw = os.environ.get("BSKY_HANDLE"), os.environ.get("BSKY_APP_PASSWORD")
    if h and pw:
        r = fetch("https://bsky.social/xrpc/com.atproto.server.createSession", method="POST",
                  body={"identifier": h, "password": pw})
        if r is not None:
            return "https://bsky.social", {"Authorization": "Bearer " + r.json()["accessJwt"]}, "ログインあり"
        print("  Bluesky にログインできなかった。ログインなしで読む")
    return "https://api.bsky.app", None, "ログインなし"


def bluesky(code, name, base, hdr, samples):
    since = (datetime.now(timezone.utc) - timedelta(days=28)).strftime("%Y-%m-%dT%H:%M:%SZ")
    codepat = re.compile(r"[<＜(（【\[]\s*" + re.escape(code) + r"\s*[>＞)）】\]]")
    posts = {}
    for q in (name, code):
        r = fetch(base + "/xrpc/app.bsky.feed.searchPosts", headers=hdr, wait=1.0,
                  params={"q": q, "limit": 100, "sort": "latest", "since": since, "lang": "ja"})
        if r is None:
            return None
        for p in r.json().get("posts", []):
            posts[p["uri"]] = p
    rel, seen = [], set()
    for p in posts.values():
        tx = p["record"].get("text", "")
        key = re.sub(r"\s+", "", tx)[:60]
        if key in seen:                 # 同じ文の投稿（転載・重複）は1件として数える
            continue
        # コードの括弧書きだけでは年号（例「（1835）」）も拾うので、株の話の投稿に限る
        if (codepat.search(tx) and (name in tx or "株" in tx or INVEST.search(tx))) or (name in tx and INVEST.search(tx)):
            seen.add(key)
            rel.append((p["record"].get("createdAt", "")[:19], tx))
    cut7 = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    n7 = sum(1 for t, _ in rel if t >= cut7)
    prev = (len(rel) - n7) / 3                                                # 8〜28日前の、1週あたり
    out = dict(n7=n7, wk_prev=round(prev, 1), pos=sum(1 for _, x in rel if POS.search(x)), neg=sum(1 for _, x in rel if NEG.search(x)),
               r_up=sum(1 for _, x in rel if RUP.search(x)), r_dn=sum(1 for _, x in rel if RDN.search(x)), n28=len(rel))
    samples[code] = [x.replace("\n", " ")[:90] for _, x in sorted(rel, reverse=True)[:3]]
    return out


# ---------------------------------------------------------------- まとめ
def notes(v):
    """数字を、強気・弱気・注意の短い文にする（判断はしない。見立ての材料）。"""
    up, dn, warn = [], [], []
    sh = v.get("shorts")
    if sh and sh.get("n", 0) >= 3:
        if sh["up"] > sh["down"] and sh["total"] >= 1:
            dn.append(f"機関の空売りが増えている（報告{sh['n']}件中、増やした{sh['up']}件・合計{sh['total']}%）")
        elif sh["down"] > sh["up"]:
            up.append(f"機関が空売りを減らしている（報告{sh['n']}件中、減らした{sh['down']}件）")
    w = v.get("wiki")
    if w and w.get("ratio") and w["avg7"] >= 30:
        if w["ratio"] >= 2:
            warn.append(f"世間の関心が急に高まっている（Wikipediaの閲覧が先月の{w['ratio']:.1f}倍）")
        elif w["ratio"] <= 0.5:
            warn.append(f"世間の関心が下がっている（Wikipediaの閲覧が先月の{w['ratio']:.1f}倍）")
    b = v.get("bsky")
    if b and b["n7"] >= 3:
        if b["wk_prev"] > 0 and b["n7"] >= 3 * b["wk_prev"]:
            warn.append(f"SNSで投資の話題が急に増えた（1週間で{b['n7']}件、前の3週は週{b['wk_prev']}件）")
        if b["pos"] >= 2 * max(1, b["neg"]):
            up.append(f"SNSの話題は良い話が多い（良い{b['pos']}・悪い{b['neg']}）")
        elif b["neg"] >= 2 * max(1, b["pos"]):
            dn.append(f"SNSの話題は悪い話が多い（良い{b['pos']}・悪い{b['neg']}）")
    if b and (b["r_up"] or b["r_dn"]):
        (up if b["r_up"] > b["r_dn"] else dn if b["r_dn"] > b["r_up"] else warn).append(
            f"アナリストの評価の話題：格上げ・引き上げ{b['r_up']}件、格下げ・引き下げ{b['r_dn']}件（SNSより）")
    tr = v.get("turnover")
    if tr and tr >= 1.5:
        warn.append(f"売買代金が増えている（直近5日は20日平均の{tr:.1f}倍）")
    if v.get("rate4") is not None and v["rate4"] <= -10:
        up.append(f"個人の信用買いが減っている（4週で{v['rate4']:+.0f}%）")
    return dict(up=up, down=dn, warn=warn)


def main():
    t = targets()
    if not t:
        print("対象の銘柄がない（latest.json が読めない）")
        return 1
    print(f"世間の評価 {NOW:%Y-%m-%d %H:%M}（{len(t)}銘柄）")
    codes = set(t)
    t0 = time.time()
    sh = shorts(codes)
    titles = wiki_titles(t)
    base, hdr, how = bsky_base()
    samples, res = {}, {}
    for c, v in t.items():
        e = dict(name=v["name"])
        if sh:
            e["shorts"] = sh.get(c, dict(n=0))
        if titles.get(c):
            pv = pageviews(titles[c])
            if pv:
                e["wiki"] = dict(title=titles[c], **pv)
        bs = bluesky(c, v["name"], base, hdr, samples)
        if bs is not None:
            e["bsky"] = bs
        if v.get("val5") and v.get("val20"):
            e["turnover"] = round(v["val5"] / v["val20"], 2)
        e["rate4"] = v.get("rate4")
        e["notes"] = notes(e)
        res[c] = e
    src = dict(shorts=f"JPX 空売り残高（{sh.get('_from', '')}〜{sh.get('_to', '')}公表の{sh.get('_files', 0)}日分）" if sh else "取れなかった",
               wiki="Wikipedia 閲覧数（直近7日と、その前の4週の1日平均）" if any("wiki" in e for e in res.values()) else "取れなかった",
               bsky=f"Bluesky（{how}・直近4週の投資の話題）" if any("bsky" in e for e in res.values()) else "取れなかった")
    out = dict(updated=NOW.strftime("%Y-%m-%d %H:%M"), sources=src, stocks=res,
               note="どれも株価の予想に効くかはまだ検証していません。ひとつの情報に頼らず、見立ての材料の1つとして使います。")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.makedirs(HIST, exist_ok=True)
    with open(os.path.join(HIST, f"{NOW:%Y%m%d}.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    for k, v in src.items():
        print(f"  {k}: {v}")
    for c, e in res.items():
        n = e["notes"]
        parts = []
        if e.get("shorts", {}).get("n"):
            s = e["shorts"]
            parts.append(f"空売り報告{s['n']}件(増{s['up']}/減{s['down']}・計{s['total']}%)")
        if e.get("wiki"):
            parts.append(f"Wiki{e['wiki']['avg7']}回/日(先月比{e['wiki']['ratio']})")
        if e.get("bsky"):
            b = e["bsky"]
            parts.append(f"SNS{b['n7']}件/週(前{b['wk_prev']}・良{b['pos']}悪{b['neg']}・格上{b['r_up']}格下{b['r_dn']})")
        if e.get("turnover"):
            parts.append(f"代金{e['turnover']}倍")
        print(f"[{c} {e['name'][:10]}] " + "・".join(parts))
        for lab, k in (("＋", "up"), ("－", "down"), ("！", "warn")):
            for x in n[k]:
                print(f"    {lab} {x}")
        for x in samples.get(c, []):
            print(f"    （SNS）{x}")
    print(f"書き出し: docs/data/opinion.json・data/opinion/{NOW:%Y%m%d}.json（{time.time() - t0:.0f}秒）")
    print("使い方：どれも予想に効くかは未検証。見立てでは、ひとつの情報だけで決めず、需給・業績・ニュースと合わせて判断の材料にする。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
