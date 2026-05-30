"""
日本株ADR乖離率ランキングを毎朝Xに投稿するスクリプト。
adr-data.json を取得 → ベスト3・ワースト3を1ツイートで投稿。
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timezone, timedelta

import requests
import tweepy


JST = timezone(timedelta(hours=9))
JSON_URL = "https://moo-stock-blog.com/adr-data.json"
ADR_PAGE_URL = "https://moo-stock-blog.com/%e6%97%a5%e6%9c%ac%e6%a0%aaadr/"

# 長い銘柄名の短縮表記
NAME_SHORT = {
    "三菱UFJフィナンシャル・グループ": "三菱UFJ",
    "三井住友フィナンシャルグループ": "三井住友FG",
    "三井住友トラストグループ": "三井住友信託",
    "みずほフィナンシャルグループ": "みずほFG",
    "ソフトバンクグループ": "ソフトバンクG",
    "ファーストリテイリング": "ファストリ",
    "東京エレクトロン": "東エレク",
    "ニトリホールディングス": "ニトリ",
    "セブン&アイ・ホールディングス": "セブン&i",
    "アステラス製薬": "アステラス",
    "オリエンタルランド": "OLC",
    "ANAホールディングス": "ANA",
    "Japan Post Holdings": "日本郵政",
    "MS&ADインシュアランス": "MS&AD",
    "SOMPOホールディングス": "SOMPO",
    "東京海上HD": "東京海上",
    "サントリー食品インターナショナル": "サントリーBF",
    "アサヒグループHD": "アサヒ",
    "キリンホールディングス": "キリン",
    "東京電力HD": "東電HD",
    "関西電力": "関電",
    "大和ハウス工業": "大和ハウス",
    "積水ハウス": "積水ハウス",
    "ダイキン工業": "ダイキン",
    "JR東日本": "JR東",
    "JR東海": "JR東海",
    "JR西日本": "JR西",
    "日本郵船": "郵船",
    "商船三井": "商船三井",
    "日本製鉄": "日鉄",
    "三菱重工業": "三菱重",
    "川崎重工業": "川崎重",
    "信越化学工業": "信越化学",
    "住友電気工業": "住友電工",
    "住友金属鉱山": "住友金属鉱",
    "三井金属鉱業": "三井金属鉱",
    "中外製薬": "中外製薬",
    "リクルートHD": "リクルート",
    "ENEOSホールディングス": "ENEOS",
    "アドバンテスト": "アドバンテ",
    "パナソニックHD": "パナソニック",
    "三菱電機": "三菱電機",
    "ブリヂストン": "ブリヂストン",
    "Pan Pacific International": "パンパシ",
    "パン・パシフィックHD": "パンパシHD",
    "スクウェア・エニックス": "スクエニ",
    "コナミグループ": "コナミG",
    "ヤマハ発動機": "ヤマハ発",
    "リード・リアル・エステート": "リードリアル",
}


# ── 文面のゆらぎ（同じテンプレの連投によるXのスパム判定を避ける）──
# 毎回ランダムに選ぶことで、見た目が固定テンプレにならないようにする。
OPENINGS = [
    "今朝のADR動向 ({d})",
    "ADR、今朝はこんな感じ ({d})",
    "今日のADRチェック ({d})",
    "本日のADR乖離率まとめ ({d})",
    "おはようございます、今朝のADRです ({d})",
    "{d} 今朝のADRをサクッと",
    "ADR乖離率の朝メモ ({d})",
    "{d} ADRの寄り前チェック",
]

UP_HEADERS = [
    "上がってる:",
    "上昇組:",
    "買われてる:",
    "プラス勢:",
    "上げてる銘柄:",
    "上昇トップ:",
]

DOWN_HEADERS = [
    "下がってる:",
    "下落組:",
    "売られてる:",
    "マイナス勢:",
    "下げてる銘柄:",
    "下落トップ:",
]

# URLの前に付ける一言（空文字はURLのみ＝リード無し）
URL_LEADS = [
    "",
    "詳しくはこちら",
    "全銘柄はこちら",
    "続きはサイトで",
    "一覧はこちら",
]

# 冒頭に付ける絵文字（無しを多めにして、有無も含めて毎回違う見た目に）
LEAD_EMOJIS = ["", "", "", "", "📊", "📈", "🗾", "🌅"]


def fetch_data():
    """ロリポップから adr-data.json を取得（WAF回避のためUser-Agent指定）"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    r = requests.get(JSON_URL, timeout=15, headers=headers)
    r.raise_for_status()
    return r.json()


def short_name(name, max_len=10):
    """銘柄名を短く整形"""
    if name in NAME_SHORT:
        return NAME_SHORT[name]
    if len(name) <= max_len:
        return name
    return name[:max_len]


def format_line(item):
    """1行整形: '三井金属鉱業 +20.5%'（小数点第1位）"""
    name = short_name(item.get("name_jp", ""))
    pct = item.get("divergence_pct", 0.0)
    sign = "+" if pct >= 0 else ""
    return f"{name} {sign}{pct:.1f}%"


def build_tweet(data):
    """ツイート本文を組み立てる（X 280文字以内、ベスト3・ワースト3 + URL）
    文面をランダムに選んで、毎回見た目が変わるようにする（スパム判定回避）。
    """
    now = datetime.now(JST)
    date_str = f"{now.month}/{now.day}"

    opening = random.choice(OPENINGS).format(d=date_str)
    emoji = random.choice(LEAD_EMOJIS)
    if emoji:
        opening = f"{emoji} {opening}"

    lines = [opening, ""]

    lines.append(random.choice(UP_HEADERS))
    for item in data["best"][:3]:
        lines.append(format_line(item))

    lines.append("")
    lines.append(random.choice(DOWN_HEADERS))
    for item in data["worst"][:3]:
        lines.append(format_line(item))

    lines.append("")
    lead = random.choice(URL_LEADS)
    if lead:
        lines.append(f"{lead} {ADR_PAGE_URL}")
    else:
        lines.append(ADR_PAGE_URL)

    return "\n".join(lines)


def post_to_x(text):
    """X API v2 でツイート投稿"""
    api_key = os.environ["X_API_KEY"]
    api_secret = os.environ["X_API_SECRET"]
    access_token = os.environ["X_ACCESS_TOKEN"]
    access_secret = os.environ["X_ACCESS_SECRET"]

    client = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret,
    )
    return client.create_tweet(text=text)


def main():
    print(f"[info] start: {datetime.now(JST).isoformat()}", flush=True)

    try:
        data = fetch_data()
        print(f"[info] data updated_at: {data.get('updated_at')}", flush=True)
        print(f"[info] best count: {len(data.get('best', []))}", flush=True)
        print(f"[info] worst count: {len(data.get('worst', []))}", flush=True)
    except Exception as e:
        print(f"[error] fetch failed: {e}", file=sys.stderr, flush=True)
        return 1

    if not data.get("best") or not data.get("worst"):
        print("[error] best/worst data missing", file=sys.stderr, flush=True)
        return 1

    tweet_text = build_tweet(data)
    print(f"[info] tweet length: {len(tweet_text)} python-chars", flush=True)
    print("[info] tweet preview:", flush=True)
    print("-" * 40, flush=True)
    print(tweet_text, flush=True)
    print("-" * 40, flush=True)

    try:
        result = post_to_x(tweet_text)
        print(f"[info] posted successfully: id={result.data.get('id') if result.data else 'unknown'}", flush=True)
        return 0
    except Exception as e:
        print(f"[error] post failed: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
