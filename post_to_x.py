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

★ キーワードベース優先投稿（2026/05/25 追加）★
  keywords.json があれば、過去にバズった単語を多く含む候補を優先投稿する。
  - 全アカウントの候補を集約 → 新しい順に MAX_TRANSLATE_PER_RUN 件だけ翻訳
  - 翻訳済み本文に含まれるキーワード数でスコアリング
  - スコア降順 → 同点なら新しい順でソート → 上から投稿チェック
  - keywords.json が無ければスコアリング無効（=実質、新しい順で投稿）
  - 翻訳コスト天井: MAX_TRANSLATE_PER_RUN=3 → 1回最大$0.003、月最大$8.6

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
MAX_POSTS_PER_DAY   = 5       # 1日あたりの投稿上限
MAX_POSTS_PER_MONTH = 140     # 1か月あたりの投稿上限
                              # ※ 2026/05/25 変更: 全投稿URLなしのため、
                              #    140件 × $0.015 = $2.10/月 (予算$5の42%)
                              #    余裕あるが上限はそのまま140件で運用
MIN_INTERVAL_SEC    = 900     # 投稿と投稿の最小間隔(秒) = 15分（cron間隔と一致）

POSTING_HOUR_START_JST = 6    # 投稿OK開始時刻（JST、0-23の整数）
POSTING_HOUR_END_JST   = 23   # 投稿OK終了時刻（JST、これ未満ならOK）

# ─── URL頻度のコントロール（コスト最適化のキモ）───
URL_EVERY_N_POSTS = 999       # ★ 2026/05/25 変更: 全投稿URLなし に変更
                              # 旧: 10 (1/10 にURL付き) → 新: 999 (実質URLなし)
                              # ※ should_include_url() で強制的に False を返すように
                              #   実装側で固定。この設定値はコスト計算用にのみ使用。
                              # 1=毎回、2=半分、3=1/3、5=1/5、10=1/10、999=ほぼ無し

# X API 単価（2026/04/20 改定後の実コスト。Developer Console で要確認）
COST_PER_URL_POST_USD   = 0.20    # URL付き投稿の単価
COST_PER_PLAIN_POST_USD = 0.015   # URLなし投稿の単価

# 実効単価（URL_EVERY_N_POSTS から自動計算）
ESTIMATED_COST_PER_POST_USD = (
    COST_PER_URL_POST_USD / URL_EVERY_N_POSTS
    + COST_PER_PLAIN_POST_USD * (URL_EVERY_N_POSTS - 1) / URL_EVERY_N_POSTS
)
MONTHLY_BUDGET_USD = 5.0      # 月予算($) 超えたら停止

# ─── キーワードベース優先投稿（2026/05/25 追加）───
KEYWORDS_PATH = "keywords.json"
MAX_TRANSLATE_PER_RUN = 3    # 1回の実行で翻訳する最大候補数（翻訳コスト天井）
                              # 候補が多くてもこの件数までしか翻訳→スコアリングしない
                              # コスト試算: $0.001/件 × 3 × 96回/日 × 30日 ≈ $8.6/月 上限

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

    # (1) 時間帯チェック
    if not (POSTING_HOUR_START_JST <= now.hour < POSTING_HOUR_END_JST):
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

    # (4) 月予算チェック（実コスト + 次の1投稿のコストで判定）
    actual_cost = _calc_actual_monthly_cost(state)
    # ★ 2026/05/25 変更: 全投稿URLなし固定 ($0.015) なので、URLなし単価で判定
    if actual_cost + COST_PER_PLAIN_POST_USD > MONTHLY_BUDGET_USD:
        return False, (
            f"今月の実コスト${actual_cost:.3f} + 次投稿$0.015 が予算${MONTHLY_BUDGET_USD:.2f}を超過"
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
                        "【最重要・絶対厳守】人物名に勝手な肩書きを付けるのは絶対に禁止。\n"
                        "原文に明示されている肩書きだけを訳すこと。「氏」「さん」も同様に追加禁止。\n"
                        "\n"
                        "  ✅ 正しい例:\n"
                        "    原文 'Trump says X'              → 訳『トランプはXと発言』\n"
                        "    原文 'Trump:'                    → 訳『トランプ:』\n"
                        "    原文 'President Trump says X'    → 訳『トランプ大統領はXと発言』\n"
                        "    原文 'Former President Trump'    → 訳『トランプ前大統領』\n"
                        "    原文 'Powell says X'             → 訳『パウエルはXと発言』\n"
                        "    原文 'Fed Chair Powell'          → 訳『FRB議長パウエル』\n"
                        "    原文 'BOJ Governor Ueda'         → 訳『日銀総裁植田』\n"
                        "\n"
                        "  ❌ 間違いの例 (絶対にやらないこと):\n"
                        "    原文 'Trump says X'              → ✗『トランプ前大統領はXと発言』\n"
                        "    原文 'Trump says X'              → ✗『トランプ氏はXと発言』\n"
                        "    原文 'Trump says X'              → ✗『トランプ大統領はXと発言』\n"
                        "    原文 'Trump says X'              → ✗『トランプ元大統領はXと発言』\n"
                        "    原文 'Powell says X'             → ✗『パウエル議長はXと発言』\n"
                        "    原文 'Powell says X'             → ✗『パウエル氏はXと発言』\n"
                        "\n"
                        "原文に書かれていない肩書き(『前大統領』『元大統領』『現大統領』『大統領』\n"
                        "『議長』『総裁』『首相』『氏』『さん』など)を翻訳結果に追加することは\n"
                        "絶対に禁止。原文が単に名前だけなら、訳も名前だけ。\n"
                        "\n"
                        "【その他のルール】\n"
                        "1. 翻訳結果のみを出力し、説明や前置きは一切付けないこと。\n"
                        "2. ニュース速報らしい簡潔な表現にすること。\n"
                        "3. 原文に書かれていない情報を一切補足しないこと。\n"
                        "4. 国名・組織名・通貨も原文に書かれている範囲で訳す。\n"
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

    ★ 2026/05/25 変更: コスト削減のため、常に False を返す (全投稿URLなし)。
       URL頻度の動的制御は無効化。
       旧仕様: URL_EVERY_N_POSTS=10 で 1/10 にURL付き
       新仕様: 全件 URLなし ($0.015/件)
    """
    return False


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
# キーワード読込・スコアリング（2026/05/25 追加）
# ───────────────────────────────────────────────

def load_keywords() -> List[str]:
    """keywords.json から単語リストを返す。無ければ空リスト（後方互換）。"""
    if not os.path.exists(KEYWORDS_PATH):
        log(f"[INFO] {KEYWORDS_PATH} なし → スコアリング無効、新しい順で投稿")
        return []
    try:
        with open(KEYWORDS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        words = [k.get("word", "") for k in data.get("keywords", []) if isinstance(k, dict)]
        words = [w for w in words if w]
        log(f"[INFO] {KEYWORDS_PATH} 読込: {len(words)}語")
        return words
    except Exception as e:
        log(f"[WARN] {KEYWORDS_PATH} 読込失敗: {type(e).__name__}: {e} → スコアリング無効")
        return []


def score_text(text: str, keywords: List[str]) -> int:
    """テキストに keywords の語が何個含まれているかカウント。"""
    if not keywords or not text:
        return 0
    return sum(1 for kw in keywords if kw in text)


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

    # ── キーワード読込（無ければスコアリング無効で従来動作）
    keywords = load_keywords()

    # ── 全アカウントから候補を集約
    all_candidates: List[Dict] = []  # [{"tweet": ..., "acc": ...}, ...]
    for i, acc in enumerate(ACCOUNT_CONFIGS):
        if i > 0:
            time.sleep(5)  # アカウント間レート制限避け
        handle = acc["handle"]
        log(f"[INFO] @{handle} の取得開始")
        tweets, status = fetch_tweets_via_twitterapi_io(handle, twitterapi_key)
        log(f"[INFO] @{handle} status={status}, raw={len(tweets)}")
        for t in tweets:
            all_candidates.append({"tweet": t, "acc": acc})

    log(f"[INFO] 全候補(生): {len(all_candidates)}件")

    # ── 適格な候補だけ残す（未投稿 / 非リプ / 非RT / フィルタ通過）
    eligible: List[Dict] = []
    for c in all_candidates:
        t = c["tweet"]
        acc = c["acc"]
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
        eligible.append(c)

    log(f"[INFO] 適格候補: {len(eligible)}件")

    if not eligible:
        log("[INFO] 投稿可能な新規候補なし")
        state["posted_ids"] = list(posted_ids)
        save_state(state)
        return 0

    # ── 新しい順にソート、上位 MAX_TRANSLATE_PER_RUN 件だけ翻訳（コスト天井）
    eligible.sort(key=lambda c: c["tweet"].get("createdAt") or "", reverse=True)
    to_process = eligible[:MAX_TRANSLATE_PER_RUN]
    log(f"[INFO] 翻訳対象: 上位{len(to_process)}件（上限{MAX_TRANSLATE_PER_RUN}件）")

    # ── 翻訳 + スコアリング
    scored: List[Dict] = []
    for c in to_process:
        t = c["tweet"]
        acc = c["acc"]
        tid = str(t.get("id"))
        text = t.get("text") or ""
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

        # スコアリング（キーワード含有数）
        score = score_text(body_clean, keywords)
        scored.append({
            "tweet": t,
            "acc": acc,
            "body_clean": body_clean,
            "score": score,
            "created_at": t.get("createdAt") or "",
        })

    if not scored:
        log("[INFO] 翻訳・整形後の候補が0件")
        state["posted_ids"] = list(posted_ids)
        save_state(state)
        return 0

    # ── スコア降順、同点なら新しい順でソート
    #    Python の sort は stable なので、新しい順 → スコア降順 の順でかける
    scored.sort(key=lambda c: c["created_at"], reverse=True)
    scored.sort(key=lambda c: c["score"], reverse=True)

    # ── スコアリング結果のログ
    log(f"[INFO] スコアリング結果(降順, keywords={len(keywords)}語):")
    for i, c in enumerate(scored, 1):
        log(f"  {i}. score={c['score']} @{c['acc']['handle']} {c['body_clean'][:50]}")

    # ── 上から順に投稿チェック → 最初に通った1件を投稿
    posts_this_run = 0
    rate_limited = False
    for c in scored:
        if posts_this_run >= MAX_POSTS_PER_RUN:
            log(f"[INFO] 1回の上限({MAX_POSTS_PER_RUN}件)到達 → 中断")
            break

        # ★ 投稿ごとに再判定（最小間隔・日次上限）
        ok_each, reason_each = can_post_now(state)
        if not ok_each:
            log(f"[STOP] ループ内ガード: {reason_each}")
            rate_limited = True
            break

        t = c["tweet"]
        tid = str(t.get("id"))
        body_clean = c["body_clean"]
        handle = c["acc"]["handle"]
        score = c["score"]

        # ★ URL付きにするかどうか判定
        include_url = should_include_url(state)
        tweet_text = format_tweet(body_clean, handle, include_url)
        cost_tag = "URL付き($0.20)" if include_url else "URLなし($0.015)"

        # 投稿
        log(f"[POST] @{handle} src_id={tid} score={score} {cost_tag} body={body_clean[:50]}")
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
