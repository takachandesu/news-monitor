"""
post_to_x.py

@FirstSquawk と @Yuto_Headline の速報ツイートを取得し、
moo-stock-blog の運用 X アカウントに自動投稿するスクリプト。

GitHub Actions から10分おきに呼ばれる前提（環境変数で認証情報を受け取る）。

仕様:
  - FirstSquawk: 全件速報扱い → Claude Haiku 4.5 で日本語翻訳して投稿
  - Yuto_Headline: 「*」または「＊」で始まる速報のみ → 日本語なので翻訳しない
  - posted_state.json で投稿済み tweet_id を管理（直近300件保持）
  - 1回の実行で最大10件まで投稿（暴走防止）
  - 翻訳失敗（日本語化できなかった）場合は投稿スキップ

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
from typing import Dict, List, Tuple

import requests
import tweepy
import anthropic


# ───────────────────────────────────────────────
# 定数
# ───────────────────────────────────────────────

STATE_PATH = "posted_state.json"
MAX_STATE_ENTRIES = 300    # state.json に保持する tweet_id の最大件数
MAX_POSTS_PER_RUN = 10     # 1回の実行で投稿する上限（暴走防止）
LOOKBACK_MINUTES = 60      # TwitterAPI.io 検索の遡及範囲（分）
SLEEP_BETWEEN_POSTS = 2    # 投稿の間隔（秒）
BLOG_URL = "https://moo-stock-blog.com/news-headline-monitor/"
BODY_MAX_CHARS = 80        # 投稿本文の最大文字数（X 280文字制限に余裕を持たせる）

# アカウント設定（app.py の account_configs と整合）
ACCOUNT_CONFIGS = [
    {
        "handle": "FirstSquawk",
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

def load_state() -> Dict:
    """posted_state.json を読み込み。存在しなければ空状態を返す。"""
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    return {"posted_ids": [], "last_run_at": None}
                data.setdefault("posted_ids", [])
                data.setdefault("last_run_at", None)
                return data
        except Exception as e:
            log(f"[WARN] state.json 読込失敗: {e}. 空状態でスタート")
    return {"posted_ids": [], "last_run_at": None}


def save_state(state: Dict) -> None:
    """投稿済み tweet_id は直近 MAX_STATE_ENTRIES 件だけ保持する。"""
    state["posted_ids"] = list(state.get("posted_ids", []))[-MAX_STATE_ENTRIES:]
    state["last_run_at"] = int(time.time())
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ───────────────────────────────────────────────
# TwitterAPI.io 経由でツイート取得（app.py の _twitterapi_io_search 相当）
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
# Claude による翻訳（app.py の _translate_to_japanese 相当）
# ───────────────────────────────────────────────

def translate_to_japanese(english_text: str, claude_client: anthropic.Anthropic) -> str:
    """英文を日本語に翻訳。失敗時は元の英文を返す。"""
    if not english_text or not english_text.strip():
        return english_text

    # 数値・記号だけは翻訳しない（コスト節約）
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
                        "次の英語の金融速報ヘッドラインを、自然な日本語に翻訳してください。"
                        "翻訳結果のみを出力し、説明や前置きは一切付けないでください。"
                        "ニュース速報らしい簡潔な表現でお願いします。\n\n"
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
    # 改行はスペースに（X 上での見栄え）
    cleaned = " ".join(cleaned.split())
    return cleaned


def format_tweet(body: str, handle: str) -> str:
    """
    投稿フォーマット：

        🔴速報
        [本文]

        詳細は以下News Headline Monitorをクリック。Xよりも早くニュースがでます。
        https://moo-stock-blog.com/news-headline-monitor/
    """
    if len(body) > BODY_MAX_CHARS:
        body = body[:BODY_MAX_CHARS - 1] + "…"

    return (
        "🔴速報\n"
        f"{body}\n\n"
        "詳細は以下News Headline Monitorをクリック。Xよりも早くニュースがでます。\n"
        f"{BLOG_URL}"
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

    # ── クライアント初期化
    claude_client = anthropic.Anthropic(api_key=claude_key)
    x_client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_SECRET"],
    )

    # ── 状態読込
    state = load_state()
    posted_ids = set(state.get("posted_ids", []))
    log(f"[INFO] 開始 / 過去投稿済み: {len(posted_ids)} 件")

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

            # 本文の1行目を切り出し（app.py と同じロジック）
            first_line = text.split("\n", 1)[0].strip()
            raw_body = first_line[:160] if first_line else text[:160]

            # 翻訳
            if acc["translate"]:
                body_ja = translate_to_japanese(raw_body, claude_client)
                # 翻訳が日本語にならなかった場合は投稿しない
                if not looks_like_japanese(body_ja):
                    log(f"[SKIP] 翻訳失敗の可能性, tweet_id={tid}, body={raw_body[:50]}")
                    continue
            else:
                body_ja = raw_body

            # 整形
            body_clean = clean_for_post(body_ja)
            if not body_clean:
                continue

            tweet_text = format_tweet(body_clean, handle)

            # 投稿
            log(f"[POST] @{handle} src_id={tid} body={body_clean[:50]}")
            ok, info = post_to_x(tweet_text, x_client)

            if ok:
                posted_ids.add(tid)
                posts_this_run += 1
                # 投稿の都度 state を保存（途中失敗時の二重投稿を緩和）
                state["posted_ids"] = list(posted_ids)
                save_state(state)
                log(f"[OK]   posted, new_x_id={info}")
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
    log(f"[INFO] 終了 / 今回の新規投稿: {posts_this_run} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
