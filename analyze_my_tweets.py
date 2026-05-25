"""
analyze_my_tweets.py

@moo_stock の過去ツイートを分析し、IMPRESSION_THRESHOLD 閲覧以上の投稿から
頻出単語（名詞）を抽出して keywords.json に保存する。

このスクリプトは X 自動投稿のフロー（post_to_x.py）には一切干渉しない。
単独で実行され、keywords.json を生成するだけ。
post_to_x.py がそれをどう使うかはフェーズ3で実装する。

実行例:
  python analyze_my_tweets.py                # 過去90日を分析
  python analyze_my_tweets.py --days 30      # 過去30日を分析
  python analyze_my_tweets.py --threshold 100  # 閾値を100閲覧に下げる

環境変数:
  TWITTERAPI_IO_KEY: TwitterAPI.io のAPIキー（既存と共用）
"""

import os
import sys
import json
import time
import argparse
import requests
from datetime import datetime, timezone, timedelta
from collections import Counter
from typing import Dict, List, Set, Tuple

# ═══════════════════════════════════════════════════════
# 設定（必要に応じて調整）
# ═══════════════════════════════════════════════════════

HANDLE = "moo_stock"                  # 分析対象アカウント
IMPRESSION_THRESHOLD = 200            # 「伸びた」と判定する閲覧数
ANALYSIS_DAYS = 90                    # 過去何日分析するか（デフォルト）
TOP_N_KEYWORDS = 20                   # 抽出する単語数
KEYWORDS_PATH = "keywords.json"
MAX_PAGES = 10                        # TwitterAPI.io 取得ページ数上限
PAGE_SLEEP_SEC = 2                    # ページ間のレート制限避け
JST = timezone(timedelta(hours=9))

# ストップワード（除外する一般語）
STOPWORDS: Set[str] = {
    # 自動投稿テンプレに含まれるノイズ語
    "速報", "詳細", "プロフィール", "固定", "News", "Headline", "Monitor",
    "クリック", "より", "ニュース", "以下", "リンク",
    # 一般的な指示語・形式名詞
    "もの", "こと", "それ", "これ", "あれ", "どれ", "やつ", "ところ",
    "ため", "など", "とき", "うち", "わけ", "場合", "通り", "向け",
    # 時間表現
    "今", "今日", "今月", "前", "後", "間", "中", "上", "下", "次",
    "本日", "明日", "昨日", "今週", "先週", "来週",
    # 通貨・単位（単独だと意味ない）
    "件", "億", "万", "千", "円", "ドル", "％", "％",
    # よくある日本語助詞・付属語
    "等", "他", "全", "可能", "実施", "発表",
}


# ═══════════════════════════════════════════════════════

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fetch_my_tweets(api_key: str, days: int) -> List[Dict]:
    """TwitterAPI.io から @HANDLE の過去 days 日分のツイートを取得（ページング対応）。"""
    now = int(time.time())
    since_ts = now - days * 86400
    since_str = time.strftime("%Y-%m-%d_%H:%M:%S_UTC", time.gmtime(since_ts))
    query = f"from:{HANDLE} since:{since_str}"

    all_tweets: List[Dict] = []
    cursor = None

    for page in range(MAX_PAGES):
        base_url = (
            "https://api.twitterapi.io/twitter/tweet/advanced_search"
            f"?query={requests.utils.quote(query)}&queryType=Latest"
        )
        url = f"{base_url}&cursor={cursor}" if cursor else base_url
        headers = {"X-API-Key": api_key}

        log(f"[INFO] ページ {page + 1} 取得中…")
        try:
            r = requests.get(url, headers=headers, timeout=30)
        except Exception as e:
            log(f"[ERR] 取得失敗: {type(e).__name__}: {e}")
            break

        if r.status_code != 200:
            log(f"[ERR] HTTP {r.status_code}: {r.text[:200]}")
            break

        try:
            data = r.json()
        except Exception:
            log(f"[ERR] JSONパース失敗")
            break

        tweets = data.get("tweets") or []
        if not tweets:
            log(f"[INFO] ページ{page + 1}: ツイート無し → 終了")
            break

        all_tweets.extend(tweets)
        log(f"[INFO] +{len(tweets)} 件（累計 {len(all_tweets)} 件）")

        # next cursor のキー名はTwitterAPI.io のレスポンス次第（複数候補を試す）
        cursor = data.get("next_cursor") or data.get("nextCursor") or data.get("has_next_page_cursor")
        if not cursor:
            log(f"[INFO] カーソル無し → 終了")
            break

        time.sleep(PAGE_SLEEP_SEC)

    log(f"[INFO] 合計 {len(all_tweets)} ツイート取得完了")
    return all_tweets


def get_impression_count(tweet: Dict) -> int:
    """ツイートから impression_count（閲覧数）を抽出。
    
    TwitterAPI.io のレスポンス構造は事前に確証がないので、複数のフィールド名を試す。
    取れなければ -1 を返す。
    """
    candidates = [
        tweet.get("viewCount"),
        tweet.get("view_count"),
        tweet.get("impression_count"),
        tweet.get("impressionCount"),
        tweet.get("views"),
        (tweet.get("public_metrics") or {}).get("impression_count"),
        (tweet.get("publicMetrics") or {}).get("impression_count"),
    ]
    for c in candidates:
        if c is None:
            continue
        try:
            return int(c)
        except (TypeError, ValueError):
            continue
    return -1


def filter_popular(tweets: List[Dict], threshold: int) -> Tuple[List[Dict], int]:
    """threshold 閲覧以上のツイートを抽出。
    Returns: (popular_tweets, impression_count_取れなかった件数)
    """
    popular = []
    no_imp_count = 0
    for t in tweets:
        imp = get_impression_count(t)
        if imp == -1:
            no_imp_count += 1
            continue
        if imp >= threshold:
            t["_impression"] = imp
            popular.append(t)
    return popular, no_imp_count


def extract_keywords(tweets: List[Dict]) -> List[Tuple[str, int]]:
    """ツイート本文から名詞を抽出し、頻度ランキングを返す。"""
    try:
        from janome.tokenizer import Tokenizer
    except ImportError:
        log("[ERR] janome が未インストール: pip install janome")
        sys.exit(1)

    tokenizer = Tokenizer()
    counter: Counter[str] = Counter()

    for t in tweets:
        text = t.get("text") or ""
        # URL除去
        text = " ".join(w for w in text.split() if not w.startswith("http"))
        # トークナイズ
        for token in tokenizer.tokenize(text):
            pos = token.part_of_speech.split(",")
            surface = token.surface
            # 名詞のみ
            if pos[0] != "名詞":
                continue
            # 1文字の単語は除外
            if len(surface) < 2:
                continue
            # ストップワード除外
            if surface in STOPWORDS:
                continue
            # 純粋な数字は除外
            if surface.replace(",", "").replace(".", "").isdigit():
                continue
            # 「数」「副詞可能」「非自立」「代名詞」を除く
            if len(pos) > 1 and pos[1] in ("数", "副詞可能", "非自立", "代名詞"):
                continue
            counter[surface] += 1

    return counter.most_common(TOP_N_KEYWORDS)


def save_keywords(keywords: List[Tuple[str, int]], stats: Dict) -> None:
    """keywords.json に保存。"""
    data = {
        "updated_at": int(time.time()),
        "updated_at_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "handle": HANDLE,
        "threshold": IMPRESSION_THRESHOLD,
        "stats": stats,
        "keywords": [{"word": w, "count": c} for w, c in keywords],
    }
    with open(KEYWORDS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"[INFO] {KEYWORDS_PATH} に保存完了")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=ANALYSIS_DAYS,
                        help="過去何日分析するか（デフォルト: %(default)s）")
    parser.add_argument("--threshold", type=int, default=IMPRESSION_THRESHOLD,
                        help="閲覧数の閾値（デフォルト: %(default)s）")
    args = parser.parse_args()

    api_key = os.environ.get("TWITTERAPI_IO_KEY")
    if not api_key:
        log("[ERR] 環境変数 TWITTERAPI_IO_KEY が未設定")
        return 1

    log(f"[INFO] @{HANDLE} の過去 {args.days} 日を分析開始 (閾値: {args.threshold}閲覧)")

    # 1. ツイート取得
    tweets = fetch_my_tweets(api_key, args.days)
    if not tweets:
        log("[ERR] ツイートが1件も取得できなかった")
        return 1

    # 2. 閲覧数フィルタ
    popular, no_imp = filter_popular(tweets, args.threshold)

    log("")
    log("══════════════════════════════════════════════")
    log(f" 分析結果サマリ")
    log("══════════════════════════════════════════════")
    log(f"  期間            : 過去{args.days}日")
    log(f"  取得ツイート数  : {len(tweets)}")
    log(f"  imp取得失敗     : {no_imp}件 ← 多いとTwitterAPI.io側で取れない")
    log(f"  {args.threshold}閲覧以上 : {len(popular)}件")
    log("")

    # impression_countが取れなかった割合をチェック
    if no_imp == len(tweets):
        log("[WARN] !!!!! 全ツイートで impression_count が取れていない !!!!!")
        log("[WARN] TwitterAPI.io が閲覧数を返さない仕様の可能性。")
        log("[WARN] X 公式 API を使う必要があるかも。")

    if not popular:
        log("[WARN] 閾値以上のツイートが見つからなかった")
        log("[INFO] フォールバック: 全ツイートで単語分析を実施")
        popular = tweets

    # 3. 200+ツイートTop10をレポート
    log("─── 閲覧数Top10 ───")
    sorted_pop = sorted(popular, key=lambda t: t.get("_impression", 0), reverse=True)
    for i, t in enumerate(sorted_pop[:10], 1):
        imp = t.get("_impression", "?")
        text = (t.get("text") or "")[:80].replace("\n", " ")
        log(f"  {i:2d}. [{imp}imp] {text}")
    log("")

    # 4. 単語抽出
    keywords = extract_keywords(popular)

    log(f"─── 頻出単語Top{TOP_N_KEYWORDS} ───")
    for i, (word, count) in enumerate(keywords, 1):
        log(f"  {i:2d}. {word} ({count}回)")
    log("")

    # 5. 保存
    stats = {
        "total_tweets_fetched": len(tweets),
        "popular_tweets_count": len(popular),
        "impression_unavailable_count": no_imp,
        "analysis_days": args.days,
    }
    save_keywords(keywords, stats)

    log("══════════════════════════════════════════════")
    log("[INFO] 完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
