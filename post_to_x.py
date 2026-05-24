"""
post_to_x.py

@FirstSquawk と @Yuto_Headline の速報ツイートを取得し、
moo-stock-blog の運用 X アカウントに自動投稿するスクリプト。

GitHub Actions から10分おきに呼ばれる前提（環境変数で認証情報を受け取る）。

仕様:
  - FirstSquawk: 全件速報扱い → Claude Haiku 4.5 で日本語翻訳して投稿
  - Yuto_Headline: 「*」または「＊」で始まる速報のみ → 日本語なので翻訳しない
  - posted_state.json で投稿済み tweet_id、日次/月次の投稿回数、最終投稿時刻を管理
  - 安全装置: 1日上限/1回上限/最小間隔/時間帯/月予算 を多段ガード
  - 翻訳失敗（日本語化できなかった）場合は投稿スキップ

★ コスト最適化（2026/05 改定）★
  X API は本文にURLを含む投稿が $0.20/件、含まないと $0.015/件 (13倍差)。
  そこで N回に1回だけURL付きフォーマットを使い、実効単価を大幅に下げる。
  URL_EVERY_N_POSTS=10 で実効単価は約 $0.0335/件 (URL毎回より約 6 倍安い)。

環境変数:
  TWITTERAPI_IO_KEY   TwitterAPI.io の API キー（既存と共用可）
  CLAUDE_API_KEY      Anthropic Claude API キー（既存と共用可）
  X_API_KEY           X 公式 API の API Key
  X_API_SECRET        X 公式 API の API Key Secret
  X_ACCESS_TOKEN      X 公式 API の Access Token
  X_ACCESS_SECRET     X 公式 API の Access Token Secret
"""

import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple

import requests
import tweepy
import anthropic


# ═══════════════════════════════════════════════════════
# ★ 安全装置の設定（必要に応じてここだけ調整すればOK）★
# ═══════════════════════════════════════════════════════

MAX_POSTS_PER_RUN   = 1       # 1回の実行で投稿する最大件数
MAX_POSTS_PER_DAY   = 7       # 1日あたりの投稿上限
MAX_POSTS_PER_MONTH = 200     # 1か月あたりの投稿上限
                              # ※ URL頻度1/20で 200件 × $0.0243 ≒ $4.85/月
                              #    月予算$5の範囲内
MIN_INTERVAL_SEC    = 900     # 投稿と投稿の最小間隔(秒) = 15分（cron間隔と一致）

POSTING_HOUR_START_JST = 5    # 投稿OK開始時刻（JST、0-23の整数）
POSTING_HOUR_END_JST   = 1    # 投稿OK終了時刻（JST、これ未満ならOK）
                              # ※ START < END なら通常時間帯（例 5-23 → 5:00～22:59）
                              # ※ START > END なら日をまたぐ（例 5-1 → 5:00～翌0:59）

# ─── URL頻度のコントロール（コスト最適化のキモ）───
URL_EVERY_N_POSTS = 20        # N回に1回だけ本文にブログURLを入れる
                              # 1=毎回、2=半分、3=1/3、5=1/5、10=1/10、20=1/20、999=ほぼ無し

# X API 単価（2026/04/20 改定後の実コスト。Developer Console で要確認）
COST_PER_URL_POST_USD   = 0.20    # URL付き投稿の単価
COST_PER_PLAIN_POST_USD = 0.015   # URLなし投稿の単価

# 実効単価（URL_EVERY_N_POSTS から自動計算）
ESTIMATED_COST_PER_POST_USD = (
    COST_PER_URL_POST_USD / URL_EVERY_N_POSTS
    + COST_PER_PLAIN_POST_USD * (URL_EVERY_N_POSTS - 1) / URL_EVERY_N_POSTS
)
MONTHLY_BUDGET_USD = 5.0      # 月予算($) 超えたら停止

# ═══════════════════════════════════════════════════════

STATE_PATH = "posted_state.json"
MAX_STATE_ENTRIES = 300       # state.json に保持する tweet_id の最大件数
LOOKBACK_MINUTES = 60         # TwitterAPI.io 検索の遡及範囲（分）
SLEEP_BETWEEN_POSTS = 3       # post間の最小pause（秒、上記MIN_INTERVALとは別）
BLOG_URL = "https://moo-stock-blog.com/news-headline-monitor/"
BODY_MAX_CHARS = 80           # 投稿本文の最大文字数

JST = timezone(timedelta(hours=9))

# アカウント設定（app.py の account_configs と整合）
ACCOUNT_CONFIGS = [
    {
        "handle": "FirstSquawk",
        "filter": "none",       # 全件通過
        "translate": True,      # 英語 → 日本語
    },
    {
        "handle": "unusual_whales",
        "filter": "none",       # 全件通過
        "translate": True,      # 英語 → 日本語
    },
    {
        "handle": "Yuto_Headline",
        "filter": "asterisk",   # * または ＊ で始まるツイートのみ
        "translate": False,
    },
]


# ───────────────────────────────────────────────
# ロギング
# ───────────────────────────────────────────────

def log(msg: str) -> None:
    """GitHub Actions のログに出すための shortcut。"""
    print(msg, file=sys.stderr, flush=True)


# ───────────────────────────────────────────────
# 状態ファイル
# ───────────────────────────────────────────────

def _today_jst_str() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def _this_month_jst_str() -> str:
    return datetime.now(JST).strftime("%Y-%m")


def load_state() -> Dict:
    """posted_state.json を読み込み。存在しなければ空状態を返す。"""
    default = {
        "posted_ids": [],
        "last_run_at": None,
        "last_post_at": 0,           # 最終投稿の epoch 秒
        "daily_counts": {},          # {"2026-05-23": 12, ...}
        "monthly_counts": {},        # {"2026-05": 187, ...}
        "total_post_count": 0,       # 累計投稿数（URL頻度判定用、リセットしない）
        "url_post_counts": {},       # {"2026-05": 3, ...} URL付き投稿だけの月次集計
    }
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    return default
                # 既存キーは尊重、足りないキーは default で補う
                for k, v in default.items():
                    data.setdefault(k, v)
                return data
        except Exception as e:
            log(f"[WARN] state.json 読込失敗: {e}. 空状態でスタート")
    return default


def save_state(state: Dict) -> None:
    """投稿済み tweet_id は直近 MAX_STATE_ENTRIES 件、daily/monthly は古いものを掃除。"""
    state["posted_ids"] = list(state.get("posted_ids", []))[-MAX_STATE_ENTRIES:]
    state["last_run_at"] = int(time.time())

    # daily_counts: 直近31日分のみ保持
    today = _today_jst_str()
    dc = state.get("daily_counts", {})
    today_dt = datetime.strptime(today, "%Y-%m-%d").date()
    dc_pruned = {}
    for d, c in dc.items():
        try:
            dd = datetime.strptime(d, "%Y-%m-%d").date()
            if (today_dt - dd).days <= 31:
                dc_pruned[d] = c
        except Exception:
            pass
    state["daily_counts"] = dc_pruned

    # monthly_counts: 直近12か月のみ保持
    this_month = _this_month_jst_str()
    mc = state.get("monthly_counts", {})
    mc_pruned = {}
    for ym in mc.keys():
        # ざっくり過去13か月内の年-月だけ残す
        try:
            yy, mm = ym.split("-")
            if (int(yy), int(mm)) >= (int(this_month[:4]) - 1, 1):
                mc_pruned[ym] = mc[ym]
        except Exception:
            pass
    mc_pruned[this_month] = mc.get(this_month, 0)
    state["monthly_counts"] = mc_pruned

    # url_post_counts: 同様に直近13か月分のみ保持
    upc = state.get("url_post_counts", {})
    upc_pruned = {}
    for ym in upc.keys():
        try:
            yy, mm = ym.split("-")
            if (int(yy), int(mm)) >= (int(this_month[:4]) - 1, 1):
                upc_pruned[ym] = upc[ym]
        except Exception:
            pass
    upc_pruned[this_month] = upc.get(this_month, 0)
    state["url_post_counts"] = upc_pruned

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _increment_post_count(state: Dict, with_url: bool) -> None:
    """投稿成功時に呼ぶ。日次・月次カウントを加算。"""
    today = _today_jst_str()
    this_month = _this_month_jst_str()
    state["daily_counts"][today] = state["daily_counts"].get(today, 0) + 1
    state["monthly_counts"][this_month] = state["monthly_counts"].get(this_month, 0) + 1
    state["total_post_count"] = state.get("total_post_count", 0) + 1
    if with_url:
        state["url_post_counts"][this_month] = (
            state.get("url_post_counts", {}).get(this_month, 0) + 1
        )
    state["last_post_at"] = int(time.time())


def _calc_actual_monthly_cost(state: Dict) -> float:
    """今月の実コストを「URL付き件数 × $0.20 + URLなし件数 × $0.015」で計算。"""
    this_month = _this_month_jst_str()
    total = state.get("monthly_counts", {}).get(this_month, 0)
    url_count = state.get("url_post_counts", {}).get(this_month, 0)
    plain_count = max(0, total - url_count)
    return url_count * COST_PER_URL_POST_USD + plain_count * COST_PER_PLAIN_POST_USD


# ───────────────────────────────────────────────
# ★ 安全装置: 投稿してよいか判定する
# ───────────────────────────────────────────────

def can_post_now(state: Dict) -> Tuple[bool, str]:
    """
    今このタイミングで投稿してよいか判定する。
    Returns: (OKならTrue, 不可なら理由文字列)
    """
    now = datetime.now(JST)

    # (1) 時間帯チェック（日跨ぎ対応）
    #     START < END: 通常時間帯（例 6-23 → 6:00 ≤ hour < 23:00 がOK）
    #     START > END: 日をまたぐ（例 5-1 → hour ≥ 5 または hour < 1 がOK）
    #     START == END: 全時間帯NG（あえてこう書けば全停止扱い）
    if POSTING_HOUR_START_JST < POSTING_HOUR_END_JST:
        in_window = POSTING_HOUR_START_JST <= now.hour < POSTING_HOUR_END_JST
    elif POSTING_HOUR_START_JST > POSTING_HOUR_END_JST:
        in_window = now.hour >= POSTING_HOUR_START_JST or now.hour < POSTING_HOUR_END_JST
    else:
        in_window = False  # START==END は全停止
    if not in_window:
        return False, (
            f"投稿可能時間帯外 (JST {now.hour}:{now.minute:02d}, "
            f"許可: {POSTING_HOUR_START_JST}:00-{POSTING_HOUR_END_JST}:00)"
        )

    # (2) 1日上限チェック
    today = _today_jst_str()
    today_count = state.get("daily_counts", {}).get(today, 0)
    if today_count >= MAX_POSTS_PER_DAY:
        return False, f"本日({today})の上限{MAX_POSTS_PER_DAY}件に到達({today_count}件投稿済)"

    # (3) 1か月上限チェック
    this_month = _this_month_jst_str()
    month_count = state.get("monthly_counts", {}).get(this_month, 0)
    if month_count >= MAX_POSTS_PER_MONTH:
        return False, f"今月({this_month})の上限{MAX_POSTS_PER_MONTH}件に到達({month_count}件投稿済)"

    # (4) 月予算チェック（実コスト + 次の1投稿の最大コストで判定）
    actual_cost = _calc_actual_monthly_cost(state)
    # 次の1投稿が最悪URL付き($0.20)になるかもしれないので、上振れで見る
    if actual_cost + COST_PER_URL_POST_USD > MONTHLY_BUDGET_USD:
        return False, (
            f"今月の実コスト${actual_cost:.3f} + 次投稿$0.20 が予算${MONTHLY_BUDGET_USD:.2f}を超過"
        )

    # (5) 投稿間隔チェック
    last_post_at = state.get("last_post_at", 0)
    if last_post_at:
        elapsed = int(time.time()) - last_post_at
        if elapsed < MIN_INTERVAL_SEC:
            return False, (
                f"前回投稿から{elapsed}秒しか経過していない"
                f"(最小間隔{MIN_INTERVAL_SEC}秒)"
            )

    return True, ""


# ───────────────────────────────────────────────
# TwitterAPI.io 経由でツイート取得
# ───────────────────────────────────────────────

def fetch_tweets_via_twitterapi_io(handle: str, api_key: str) -> Tuple[List[Dict], str]:
    """
    handle のツイートを直近 LOOKBACK_MINUTES 分取得する。
    429 (Too Many Requests) が返ったら最大3回までリトライ。
    戻り値: (tweets, status_text)
    """
    now = int(time.time())
    since_ts = now - LOOKBACK_MINUTES * 60
    since_str = time.strftime("%Y-%m-%d_%H:%M:%S_UTC", time.gmtime(since_ts))
    query = f"from:{handle} since:{since_str}"

    url = (
        "https://api.twitterapi.io/twitter/tweet/advanced_search"
        "?query=" + requests.utils.quote(query) + "&queryType=Latest"
    )
    headers = {"X-API-Key": api_key}

    waits = [0, 8, 15]
    last_status = "unknown"

    for attempt, wait in enumerate(waits):
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get(url, headers=headers, timeout=20)
        except Exception as e:
            return [], f"exception:{type(e).__name__}"

        if 200 <= r.status_code < 300:
            try:
                data = r.json()
            except Exception:
                return [], "json_parse_error"
            tweets = data.get("tweets")
            if isinstance(tweets, list):
                return tweets, f"ok:{len(tweets)}"
            return [], "no_tweets_key"

        last_status = f"http:{r.status_code}"
        if r.status_code == 429 and attempt < len(waits) - 1:
            continue
        return [], last_status

    return [], last_status


# ───────────────────────────────────────────────
# Claude による翻訳
# ───────────────────────────────────────────────

def translate_to_japanese(english_text: str, claude_client: anthropic.Anthropic) -> str:
    """英文を日本語に翻訳。失敗時は元の英文を返す。"""
    if not english_text or not english_text.strip():
        return english_text

    letter_count = sum(1 for c in english_text if c.isalpha())
    if letter_count < 3:
        return english_text

    try:
        message = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "次の英語の金融速報ヘッドラインを、自然な日本語に翻訳してください。\n"
                        "\n"
                        "【厳守ルール】\n"
                        "1. 翻訳結果のみを出力し、説明や前置きは一切付けないこと。\n"
                        "2. ニュース速報らしい簡潔な表現にすること。\n"
                        "3. 原文に書かれていない情報を一切補足しないこと。\n"
                        "   例: 原文が 'Trump' なら『トランプ』とだけ訳す。\n"
                        "       『前大統領』『元大統領』『現大統領』『氏』『大統領』など、\n"
                        "       原文に書かれていない肩書き・敬称・属性を勝手に足さない。\n"
                        "4. 人物名は原文のまま訳す。原文が 'Powell' なら『パウエル』、\n"
                        "   'Yellen' なら『イエレン』、'Ueda' なら『植田』など。\n"
                        "   原文に役職(Fed Chair, Treasury Secretary, BOJ Governor等)が\n"
                        "   明示されている場合のみ役職を付ける。\n"
                        "5. 国名・組織名・通貨も原文に書かれている範囲で訳す。\n"
                        "\n"
                        "【原文】\n"
                        + english_text
                    ),
                }
            ],
        )
        if message.content and len(message.content) > 0:
            translated = (message.content[0].text or "").strip()
            if translated:
                return translated
        return english_text
    except Exception as e:
        log(f"[WARN] 翻訳失敗: {type(e).__name__}: {str(e)[:100]}")
        return english_text


def looks_like_japanese(text: str, min_jp_chars: int = 3) -> bool:
    """ひらがな・カタカナ・漢字が min_jp_chars 文字以上含まれているか。"""
    jp_count = 0
    for c in text:
        if ("\u3040" <= c <= "\u309f") or ("\u30a0" <= c <= "\u30ff") or ("\u4e00" <= c <= "\u9fff"):
            jp_count += 1
            if jp_count >= min_jp_chars:
                return True
    return False


# ───────────────────────────────────────────────
# フィルタとフォーマット
# ───────────────────────────────────────────────

def passes_filter(text: str, filter_type: str) -> bool:
    if filter_type == "none":
        return True
    if filter_type == "asterisk":
        stripped = text.lstrip(" \t\r\n\"'＂　")
        return stripped.startswith("*") or stripped.startswith("＊")
    return False


def is_reply_or_retweet(tweet: Dict) -> bool:
    if tweet.get("isReply"):
        return True
    text = tweet.get("text") or ""
    if text.startswith("RT @"):
        return True
    if tweet.get("retweeted_tweet"):
        return True
    return False


def clean_for_post(text: str) -> str:
    """投稿用に本文を整形。先頭の * や ＊、連続改行などを除去。"""
    cleaned = text.lstrip(" \t\r\n\"'＂　*＊").strip()
    cleaned = " ".join(cleaned.split())
    return cleaned


def should_include_url(state: Dict) -> bool:
    """次の投稿にURLを含めるかを判定。
    URL_EVERY_N_POSTS=10 なら、累計投稿数を10で割った余りが0の時にURL付き。
    つまり 1件目、11件目、21件目、... にURL付き投稿。
    """
    if URL_EVERY_N_POSTS <= 1:
        return True  # 1=毎回URL付き
    next_index = state.get("total_post_count", 0)  # 0始まりで次投稿のindex
    return (next_index % URL_EVERY_N_POSTS) == 0


def format_tweet(body: str, handle: str, include_url: bool) -> str:
    """
    投稿フォーマット（2パターン）：

    [URL付き] N回に1回:
        🔴速報
        [本文]

        詳細は以下News Headline Monitorをクリック。Xよりも早くニュースがでます。
        https://moo-stock-blog.com/news-headline-monitor/

    [URLなし] 残りN-1回:
        🔴速報
        [本文]

        詳細はプロフィール固定の「News Headline Monitor」へ
    """
    if len(body) > BODY_MAX_CHARS:
        body = body[:BODY_MAX_CHARS - 1] + "…"

    if include_url:
        return (
            "🔴速報\n"
            f"{body}\n\n"
            "詳細は以下News Headline Monitorをクリック。Xよりも早くニュースがでます。\n"
            f"{BLOG_URL}"
        )
    else:
        return (
            "🔴速報\n"
            f"{body}\n\n"
            "詳細はプロフィール固定の「News Headline Monitor」へ"
        )


# ───────────────────────────────────────────────
# X 公式 API での投稿
# ───────────────────────────────────────────────

def post_to_x(tweet_text: str, x_client: tweepy.Client) -> Tuple[bool, str]:
    """投稿成功なら (True, new_tweet_id) を、失敗なら (False, 理由) を返す。"""
    try:
        resp = x_client.create_tweet(text=tweet_text)
        new_id = resp.data.get("id") if resp.data else ""
        return True, str(new_id)
    except tweepy.errors.Forbidden as e:
        return False, f"Forbidden: {str(e)[:120]}"
    except tweepy.errors.TooManyRequests as e:
        return False, f"RateLimit: {str(e)[:120]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"


# ───────────────────────────────────────────────
# メイン
# ───────────────────────────────────────────────

def main() -> int:
    # ── 環境変数チェック
    required_envs = [
        "TWITTERAPI_IO_KEY",
        "CLAUDE_API_KEY",
        "X_API_KEY",
        "X_API_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_SECRET",
    ]
    missing = [k for k in required_envs if not os.environ.get(k)]
    if missing:
        log(f"[ERROR] 環境変数が未設定: {', '.join(missing)}")
        return 1

    twitterapi_key = os.environ["TWITTERAPI_IO_KEY"]
    claude_key = os.environ["CLAUDE_API_KEY"]

    # ── 状態読込
    state = load_state()
    posted_ids = set(state.get("posted_ids", []))
    today = _today_jst_str()
    this_month = _this_month_jst_str()
    today_count = state.get("daily_counts", {}).get(today, 0)
    month_count = state.get("monthly_counts", {}).get(this_month, 0)
    url_count   = state.get("url_post_counts", {}).get(this_month, 0)
    actual_cost = _calc_actual_monthly_cost(state)

    log(
        f"[INFO] 開始 / 投稿済み(累計): {len(posted_ids)}件 "
        f"/ 本日 {today_count}/{MAX_POSTS_PER_DAY}件 "
        f"/ 今月 {month_count}/{MAX_POSTS_PER_MONTH}件 "
        f"(URL付き{url_count}件) "
        f"/ 実コスト ${actual_cost:.3f}/${MONTHLY_BUDGET_USD:.2f} "
        f"(実効単価${ESTIMATED_COST_PER_POST_USD:.4f}/件, URL頻度1/{URL_EVERY_N_POSTS})"
    )

    # ── ★ 安全装置: 全体ガードをまず判定（取得すらしないことでコスト節約）
    ok_global, reason = can_post_now(state)
    if not ok_global:
        log(f"[STOP] 全体ガード: {reason}")
        save_state(state)
        return 0

    # ── クライアント初期化
    claude_client = anthropic.Anthropic(api_key=claude_key)
    x_client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_SECRET"],
    )

    # ── 各アカウントを処理
    posts_this_run = 0
    rate_limited = False

    for i, acc in enumerate(ACCOUNT_CONFIGS):
        if posts_this_run >= MAX_POSTS_PER_RUN or rate_limited:
            break

        if i > 0:
            time.sleep(5)  # アカウント間でレート制限避け

        handle = acc["handle"]
        log(f"[INFO] @{handle} の取得開始")
        tweets, status = fetch_tweets_via_twitterapi_io(handle, twitterapi_key)
        log(f"[INFO] @{handle} status={status}, raw={len(tweets)}")

        # 古い順にソート（時系列で投稿するため）
        tweets_sorted = sorted(tweets, key=lambda t: t.get("createdAt") or "")

        for t in tweets_sorted:
            if posts_this_run >= MAX_POSTS_PER_RUN:
                log(f"[INFO] 1回の上限({MAX_POSTS_PER_RUN}件)到達 → 中断")
                break

            # ★ 投稿ごとに再判定（最小間隔・日次上限）
            ok_each, reason_each = can_post_now(state)
            if not ok_each:
                log(f"[STOP] ループ内ガード: {reason_each}")
                rate_limited = True
                break

            tid = str(t.get("id") or "")
            if not tid:
                continue
            if tid in posted_ids:
                continue

            text = t.get("text") or ""
            if not text:
                continue
            if is_reply_or_retweet(t):
                continue
            if not passes_filter(text, acc["filter"]):
                continue

            # 本文の1行目を切り出し
            first_line = text.split("\n", 1)[0].strip()
            raw_body = first_line[:160] if first_line else text[:160]

            # 翻訳
            if acc["translate"]:
                body_ja = translate_to_japanese(raw_body, claude_client)
                if not looks_like_japanese(body_ja):
                    log(f"[SKIP] 翻訳失敗の可能性, tweet_id={tid}, body={raw_body[:50]}")
                    continue
            else:
                body_ja = raw_body

            # 整形
            body_clean = clean_for_post(body_ja)
            if not body_clean:
                continue

            # ★ URL付きにするかどうか判定
            include_url = should_include_url(state)
            tweet_text = format_tweet(body_clean, handle, include_url)
            cost_tag = "URL付き($0.20)" if include_url else "URLなし($0.015)"

            # 投稿
            log(f"[POST] @{handle} src_id={tid} {cost_tag} body={body_clean[:50]}")
            ok, info = post_to_x(tweet_text, x_client)

            if ok:
                posted_ids.add(tid)
                posts_this_run += 1
                _increment_post_count(state, with_url=include_url)
                state["posted_ids"] = list(posted_ids)
                save_state(state)
                log(f"[OK]   posted, new_x_id={info} (累計{state['total_post_count']}件目)")
                time.sleep(SLEEP_BETWEEN_POSTS)
            else:
                log(f"[ERR]  {info}")
                if info.startswith("RateLimit"):
                    log("[INFO] レート制限のため今回は中断")
                    rate_limited = True
                    break

    # ── 最終的に state を保存
    state["posted_ids"] = list(posted_ids)
    save_state(state)

    final_today = state.get("daily_counts", {}).get(today, 0)
    final_month = state.get("monthly_counts", {}).get(this_month, 0)
    final_url   = state.get("url_post_counts", {}).get(this_month, 0)
    final_cost  = _calc_actual_monthly_cost(state)
    log(
        f"[INFO] 終了 / 今回投稿: {posts_this_run}件 "
        f"/ 本日累計 {final_today}/{MAX_POSTS_PER_DAY}件 "
        f"/ 今月累計 {final_month}/{MAX_POSTS_PER_MONTH}件 "
        f"(URL付き{final_url}件) "
        f"/ 実コスト ${final_cost:.3f}/${MONTHLY_BUDGET_USD:.2f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
