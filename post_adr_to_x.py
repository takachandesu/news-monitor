"""
日本株ADR乖離率ランキングを毎朝Xに投稿するスクリプト。
adr-data.json を取得 → ベスト3・ワースト3を1ツイートで投稿。
"""
from __future__ import annotations

import os
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


MAX_DATA_AGE_HOURS = 30  # adr-data.json がこれより古ければ投稿しない
MAX_ABS_DIVERGENCE = 30.0  # 乖離率の絶対値がこれ(%)を超える銘柄は異常値として載せない


def data_age_hours(updated_at):
    """'2026-07-28 06:35:45 JST' 形式の updated_at が何時間前かを返す。読めなければ None"""
    if not updated_at:
        return None
    s = str(updated_at).replace("JST", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            ts = datetime.strptime(s[:19], fmt).replace(tzinfo=JST)
            return (datetime.now(JST) - ts).total_seconds() / 3600
        except ValueError:
            continue
    try:
        ts = datetime.fromisoformat(s)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=JST)
        return (datetime.now(JST) - ts).total_seconds() / 3600
    except ValueError:
        return None


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


TWEET_LIMIT = 280  # Xの重み付き文字数上限


def tweet_weighted_len(text):
    """Xの重み付き文字数を概算（CJK・全角・絵文字=2、それ以外=1）。URLは無し。"""
    w = 0
    for ch in text:
        cp = ord(ch)
        if (
            0x1100 <= cp <= 0x115F or
            0x2E80 <= cp <= 0x303E or
            0x3041 <= cp <= 0x33FF or
            0x3400 <= cp <= 0x4DBF or
            0x4E00 <= cp <= 0x9FFF or
            0xA000 <= cp <= 0xA4CF or
            0xAC00 <= cp <= 0xD7A3 or
            0xF900 <= cp <= 0xFAFF or
            0xFE30 <= cp <= 0xFE4F or
            0xFF00 <= cp <= 0xFF60 or
            0xFFE0 <= cp <= 0xFFE6 or
            cp >= 0x1F000
        ):
            w += 2
        else:
            w += 1
    return w


def build_tweet(data, n=4):
    """ツイート本文を組み立てる（ベスト/ワースト n 件、URLなし）"""
    now = datetime.now(JST)
    date_str = f"{now.month}/{now.day}"

    lines = [f"今朝のADR動向 ({date_str})"]

    lines.append("")
    lines.append("上がってる:")
    for item in data["best"][:n]:
        lines.append(format_line(item))

    lines.append("")
    lines.append("下がってる:")
    for item in data["worst"][:n]:
        lines.append(format_line(item))

    lines.append("")
    lines.append("ニュース詳細はプロフィールのリンクと固定ポストに。")  # URLなし=通常ポスト課金

    return "\n".join(lines)


def build_tweet_fit(data, top_n=4, min_n=3):
    """まず top_n 件で組み立て、280を超える日は min_n 件まで減らす。
    戻り値: (ツイート文, 採用件数)"""
    for n in range(top_n, min_n - 1, -1):
        text = build_tweet(data, n)
        if tweet_weighted_len(text) <= TWEET_LIMIT:
            return text, n
    return build_tweet(data, min_n), min_n


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

    # ★ 鮮度チェック (2026/10/11 追加)
    #   2026/7/28 に moo-adr-tracker のデータ更新が GitHub に自動停止され、
    #   以後 2か月以上「7/28 の同じ数字」に今日の日付を付けて投稿し続けていた。
    #   データが古い時は投稿しない(赤✕で失敗させて気づけるようにする)。
    age_h = data_age_hours(data.get("updated_at"))
    if age_h is None:
        print(f"[error] updated_at を読めないので投稿しません: {data.get('updated_at')!r}",
              file=sys.stderr, flush=True)
        return 1
    if age_h > MAX_DATA_AGE_HOURS:
        print(f"[error] データが古いので投稿しません: updated_at={data.get('updated_at')} "
              f"({age_h:.0f}時間前 / 上限{MAX_DATA_AGE_HOURS}時間)。"
              f"moo-adr-tracker の「Update ADR Data」が止まっていないか確認してください。",
              file=sys.stderr, flush=True)
        return 1
    print(f"[info] データ鮮度OK: {age_h:.1f}時間前", flush=True)

    # ★ 異常値の除外 (2026/10/11 追加)
    #   株式分割などで ADR の換算比率がずれると「大和ハウス -50.3%」のような
    #   あり得ない乖離率が出る。±MAX_ABS_DIVERGENCE% を超える銘柄はツイートに載せない。
    for side in ("best", "worst"):
        kept, dropped = [], []
        for item in data.get(side, []):
            try:
                pct = float(item.get("divergence_pct", 0.0))
            except (TypeError, ValueError):
                dropped.append(item)
                continue
            (dropped if abs(pct) > MAX_ABS_DIVERGENCE else kept).append(item)
        for item in dropped:
            print(f"[warn] 異常値のため除外: {item.get('name_jp')} {item.get('divergence_pct')}%", flush=True)
        data[side] = kept
    if len(data.get("best", [])) < 3 or len(data.get("worst", [])) < 3:
        print("[error] 異常値を除いた結果、銘柄が足りないので投稿しません", file=sys.stderr, flush=True)
        return 1

    tweet_text, used_n = build_tweet_fit(data)
    print(f"[info] tweet best/worst {used_n}件 / weighted {tweet_weighted_len(tweet_text)} / {len(tweet_text)} python-chars", flush=True)
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
