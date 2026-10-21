# kabu-shinyou（買い残減少スクリーナー）

平日9:00と12:30（日本時間）に、クラウドの定期ルーティンが予想を更新するリポジトリ。
**売買の発注・送金など、お金が動く操作は一切しない。** 分析結果をファイルに書いてpushするだけ。

## 毎回の手順（ルーティン）

1. `python3 -m pip install -q -r tools/requirements.txt`（失敗したら1回だけ再試行。クラウドでは `pip` が別のPythonを指すので必ず `python3 -m pip` を使う）
2. `git pull --rebase`
3. `timeout 540 python3 -u tools/run.py`（Bashツールの timeout は 600000 を指定する）
   - 「休場日のため更新しません」と出たら、何もせずに終了する。
   - 終了コードが0以外なら `git checkout -- docs data` で元に戻し、エラーの要点を1行で報告して終了する（推測値でファイルを作らない）。
4. 成功したら、出力（銘柄ごとの1行要約とニュース）を読み、`docs/data/notes.json` を書く（下の形式）。
5. `git add docs/data data` → `git commit -m "予想を更新 YYYY-MM-DD HH:MM（寄り付き前|前引け後）"` → `git push`。拒否されたら `git pull --rebase` して再試行。

## notes.json の形式と書き方

```json
{"updated": "2026-10-22 09:05", "market": "2〜3文", "stocks": {"2384": "1〜2文", "7974": "1〜2文"}}
```

- `updated` は run.py の出力にある「更新: 」の日時をそのまま使う（ページは同じ日付のときだけ表示する）。
- `market`：日経平均の位置、新規買いを止めるラインとの距離、異常データの除外があればその旨。
- `stocks`：本命・次点の銘柄だけ。決算予想の対応（またぐ／半分売る／全部売る）と、ニュースで気になる点があれば一言。
- run.py が出した数字（買値・損切り・利確・確率・期待値）は書き換えない・言い換えない。
- 「必ず上がる」などの断定はしない。丁寧語の短い文。絵文字は使わない。
- ニュースは「誰が何をしたか」を取り違えない。大量保有の報告「AについてBは保有割合が…と報告」は、BがAの株を増減したという意味。選んだ銘柄がBの側なら、その銘柄の株の需給とは関係ない。
- 「地合い」は市場全体の意味なので、個別企業の業績には使わない。

## ファイル

- `tools/run.py`：本体。`--force` で休場日でも実行。
- `tools/sources.py`：JPX（信用残）・株探（決算日・業績・ニュース・日次信用残）・株予報（コンセンサス）・Yahoo Finance/stooq（株価）の取得。
- `tools/model.py`：指標、選定、売買ライン、決算予想、根拠文。検証で得た数値（期待値のATR倍率など）もここ。
- `docs/index.html`：スマホ用ページ（GitHub Pages）。`docs/data/latest.json` などを毎回取りに行く。
- `docs/data/latest.json`：最新の予想。`docs/data/track.json`：過去の予想の成績記録。
- `data/margin/*.csv.gz`：日次の信用残スナップショット（4週間比較に使う）。`bad_dates.json` は異常で除外した日。

## 既知の注意点

- JPXの日次信用残に、市場全体の買い残が1日で大きく変わる異常な日がある（2026/10/20申込分は前日比−28%）。run.py は前日比8%超の日を自動で除外する。
- 株価は Yahoo Finance を優先し、取れない銘柄だけ stooq で補う。
- ネットワークは環境の Network access で次のドメインを許可しておく必要がある：
  www.jpx.co.jp, kabutan.jp, kabuyoho.jp, finance.yahoo.com, query1.finance.yahoo.com, query2.finance.yahoo.com, fc.yahoo.com, guce.yahoo.com, consent.yahoo.com, stooq.com（pip用のpypi.org等は「Allow package managers」で許可される）
