# synthetic_fx.py
# News Headline Monitor 拡張モジュール
# - 土日の合成ドル円(bitFlyer × Binance)
# - 土日の24h株価指数(sekai-kabuka.com スクレイプ)
# - 再計算: JP225(JST 15:30基準) / US100(ET 16:00基準) / USDJPY(ET 16:00基準)
#
# 設計方針:
# - app.py には一切依存せず単独動作
# - 取得失敗時は静かに空dictを返し既存機能を一切妨げない
# - キャッシュは独立変数で 60秒TTL
# - すべて try/except で囲み外に例外を投げない
from __future__ import annotations

import os
import re
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple

import requests
import streamlit as st

# =====================================================
# 設定
# =====================================================
_CACHE_TTL_SEC = 60                     # ユーザー指定: 60秒キャッシュ
JST = timezone(timedelta(hours=9))      # 日本時間

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/122.0.0.0 Safari/537.36")

# ----- 暗号資産API -----
BITFLYER_TICKER_URL = "https://api.bitflyer.com/v1/ticker?product_code=BTC_JPY"
BINANCE_TICKER_URL  = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

# ----- sekai-kabuka.com -----
SEKAI_HOME_URL = "https://sekai-kabuka.com/pc-dow30.html"
# データURLパターン (柔軟版): //や http:// にも対応
SEKAI_DATA_URL_PATTERN = re.compile(
    r'(?:https?:)?//[a-z0-9-]+\.sekai-kabuka\.com/[^\s"\'<>]+\.1\.js'
)

# ----- Yahoo Finance (再計算用) -----
# 注: Yahooの非公式chartエンドポイントを利用 (無料・認証不要)
YAHOO_CHART_BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"

# 価格帯による銘柄判定 (sekai-kabuka 内の q(...) 行を識別する用)
# (キー, lowの最小, highの最大) — 範囲はざっくり広めに取って取りこぼし防止
_INSTRUMENT_RANGES: List[Tuple[str, float, float]] = [
    ("dow",    35000, 70000),    # ダウ平均
    ("nas100", 15000, 40000),    # NASDAQ100 (ページ表示値)
    ("vix",    5,     80),       # VIX
]

# =====================================================
# 独立キャッシュ (既存の _X_REAL_CACHE 等とは完全分離)
# =====================================================
_SYN_CACHE: Dict[str, Any] = {}
_SYN_CACHE_LOCK = threading.Lock()
_LAST_SEKAI_DATA_URL: Optional[str] = None   # 前回成功した sekai-kabuka データURL

# =====================================================
# 共通ヘルパー
# =====================================================
def _now_jst() -> datetime:
    return datetime.now(JST)


def _is_weekend_jst() -> bool:
    """
    FX市場が閉まっている時間帯を「週末モード」とする
    開: 月曜 朝7:00 JST
    閉: 土曜 朝7:00 JST
    """
    now = _now_jst()
    wd = now.weekday()      # 月=0 ... 日=6
    h = now.hour
    if wd == 5 and h >= 7:      # 土 朝7時以降
        return True
    if wd == 6:                 # 日 全日
        return True
    if wd == 0 and h < 7:       # 月 朝7時前
        return True
    return False


def _safe_get(url: str, headers: Optional[dict] = None, timeout: int = 8) -> Optional[requests.Response]:
    """例外を外に投げない GET ラッパ"""
    try:
        h = {"User-Agent": _UA, "Accept": "*/*"}
        if headers:
            h.update(headers)
        r = requests.get(url, headers=h, timeout=timeout)
        if r.status_code == 200:
            return r
    except Exception:
        pass
    return None


# =====================================================
# (1) 合成 USD/JPY  =  bitFlyer BTC/JPY  ÷  Binance BTC/USDT
# =====================================================
def _fetch_btc_usdt_binance() -> Optional[float]:
    """
    BTC/USDT (≈ BTC/USD) を取得。Binanceが地域ブロックされている環境(Streamlit Cloud等)
    でも動くように、複数ソースを順に試す。
    返り値はFloat (USD建て価格)。すべて失敗ならNone。
    """
    # 各ソース: (URL, JSONからfloatを取り出す関数)
    sources = [
        # 1. Binance 本家
        ("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
         lambda j: float(j["price"])),
        # 2. Binance Vision (公開ミラー)
        ("https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT",
         lambda j: float(j["price"])),
        # 3. Bybit (USDT価格)
        ("https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT",
         lambda j: float(j["result"]["list"][0]["lastPrice"])),
        # 4. CoinGecko (BTC を USD建てで取得 - ステーブルコインなのでUSDT≒USD)
        ("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
         lambda j: float(j["bitcoin"]["usd"])),
        # 5. Coinbase (現物価格)
        ("https://api.coinbase.com/v2/prices/BTC-USD/spot",
         lambda j: float(j["data"]["amount"])),
    ]
    for url, picker in sources:
        try:
            r = _safe_get(url, timeout=5)
            if not r:
                continue
            j = r.json()
            val = picker(j)
            if val and val > 0:
                return val
        except Exception:
            continue
    return None


def _fetch_btc_jpy_bitflyer() -> Optional[float]:
    """
    BTC/JPY を取得。bitFlyer 第一優先、ダメなら他取引所/CoinGecko。
    """
    sources = [
        # 1. bitFlyer (国内現物BTC/JPY)
        ("https://api.bitflyer.com/v1/ticker?product_code=BTC_JPY",
         lambda j: float(j.get("ltp") or 0)),
        # 2. Coincheck (国内BTC/JPY)
        ("https://coincheck.com/api/ticker",
         lambda j: float(j.get("last") or 0)),
        # 3. GMOコイン (国内BTC/JPY)
        ("https://api.coin.z.com/public/v1/ticker?symbol=BTC_JPY",
         lambda j: float(j["data"][0]["last"])),
        # 4. CoinGecko (BTC JPY建て)
        ("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=jpy",
         lambda j: float(j["bitcoin"]["jpy"])),
    ]
    for url, picker in sources:
        try:
            r = _safe_get(url, timeout=5)
            if not r:
                continue
            j = r.json()
            val = picker(j)
            if val and val > 0:
                return val
        except Exception:
            continue
    return None


def _compute_synth_usdjpy() -> Optional[Dict[str, Any]]:
    btc_jpy = _fetch_btc_jpy_bitflyer()
    btc_usd = _fetch_btc_usdt_binance()
    if not btc_jpy or not btc_usd:
        # 失敗理由を残す
        return {
            "value": None,
            "btc_jpy": btc_jpy,
            "btc_usd": btc_usd,
            "error": (
                "bitFlyer & Binance 両方失敗" if (not btc_jpy and not btc_usd)
                else "bitFlyer (BTC/JPY) 取得失敗" if not btc_jpy
                else "Binance (BTC/USDT) 取得失敗"
            ),
        }
    return {
        "value": btc_jpy / btc_usd,
        "btc_jpy": btc_jpy,
        "btc_usd": btc_usd,
        "error": None,
    }


# =====================================================
# (2) sekai-kabuka.com  =  土日ダウ / NASDAQ100 / VIX
# =====================================================
SEKAI_LOCAL_URL_FILE = "sekai_data_url.txt"  # GitHub Actions が更新する自動URLファイル


def _read_url_from_local_file() -> Optional[str]:
    """
    GitHub Actions の Playwright ワーカーが書き出した sekai_data_url.txt から
    最新URLを読み取る。リポジトリのトップに置かれている前提。
    """
    try:
        if not os.path.exists(SEKAI_LOCAL_URL_FILE):
            return None
        with open(SEKAI_LOCAL_URL_FILE, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if s.startswith("http"):
                    return s
                if s.startswith("//"):
                    return "https:" + s
        return None
    except Exception:
        return None


def _discover_sekai_data_url() -> Tuple[Optional[str], str]:
    """
    sekai-kabuka.com の最新データURLを取得する。
    優先順位:
      1) sekai_data_url.txt (GitHub Actions + Playwright で自動更新される、最も新鮮)
      2) HTML抽出 (リポジトリにファイルが無い時のみ試行)
      3) Streamlit Secrets の手動URL (フォールバック)
      4) 直前回成功時のURL (キャッシュ)
    返り値: (URL, "auto"/"html"/"manual"/"cache"/"none" のラベル)
    """
    global _LAST_SEKAI_DATA_URL

    # ── 1) リポジトリ内の自動更新ファイル (最優先・最も新鮮)
    auto_url = _read_url_from_local_file()
    if auto_url:
        _LAST_SEKAI_DATA_URL = auto_url
        return auto_url, "auto"

    # ── 2) HTML を取得して正規表現抽出 (JS生成だと取れないが念のため)
    try:
        r = _safe_get(SEKAI_HOME_URL, headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ja,en-US;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://sekai-kabuka.com/",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }, timeout=10)
        if r is not None:
            for enc in ("ascii", "utf-8", "shift_jis"):
                try:
                    text = r.content.decode(enc, errors="ignore")
                except Exception:
                    continue
                m = SEKAI_DATA_URL_PATTERN.search(text)
                if m:
                    url = m.group(0)
                    if url.startswith("//"):
                        url = "https:" + url
                    _LAST_SEKAI_DATA_URL = url
                    return url, "html"
    except Exception:
        pass

    # ── 3) Streamlit Secrets の手動URL (フォールバック)
    try:
        manual = st.secrets.get("sekai_kabuka", {}).get("manual_url", "")
        if manual and isinstance(manual, str) and manual.strip():
            url = manual.strip()
            if url.startswith("//"):
                url = "https:" + url
            return url, "manual"
    except Exception:
        pass

    # ── 4) 前回成功URL (最終フォールバック)
    if _LAST_SEKAI_DATA_URL:
        return _LAST_SEKAI_DATA_URL, "cache"

    return None, "none"


# q( count, low, high, offset, samples, 'svg文字列' ) を抽出する正規表現
_Q_PATTERN = re.compile(
    r'q\s*\(\s*(\d+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,'
)


def _extract_current_from_svg(svg_text: str, low: float, high: float) -> Optional[float]:
    """
    SVGパス文字列から「最後の点のY座標」を取り出して現在値を逆算する。

    入力: q() 関数の引数部分の後ろにあるSVG文字列断片 (エンコーディングは壊れていてOK)
    アルゴリズム:
      1) 'M' (Move-to) を探す。なければ None
      2) 'M' の後ろから 'H'/'V'/'Z'/'L' などの次のSVGコマンドまでの範囲を抽出
      3) その範囲内の全数値を抽出 (区切り文字は何でも良い)
      4) 数値は x,y のペアとみなし、奇数番目(y) を集める
      5) y_min ↔ high, y_max ↔ low と対応させ、最後の y から現在値を線形補間で算出
      6) SVGはy軸が下向きなので、y_min(画面上部)=high、y_max(画面下部)=low となる

    失敗時は None を返す。呼び出し側はレンジ中央値にフォールバックする。
    """
    try:
        m_pos = svg_text.find('M')
        if m_pos < 0:
            return None

        rest = svg_text[m_pos + 1:]

        # 次のSVGパスコマンド(または引用符) までを切り取り
        end = len(rest)
        for ch in ('H', 'V', 'Z', 'L', 'C', 'S', 'Q', 'T', 'A',
                   'h', 'v', 'z', 'l', 'c', 's', 'q', 't', 'a',
                   '"', "'", ')'):
            idx = rest.find(ch)
            if 0 < idx < end:
                end = idx
        path_data = rest[:end]

        # 区切り文字に依存せず、すべての浮動小数点数を抽出
        nums = re.findall(r'\d+\.?\d*', path_data)
        if len(nums) < 4:        # 最低でも 2点(x1,y1,x2,y2)
            return None

        try:
            values = [float(n) for n in nums]
        except ValueError:
            return None

        # 偶数番目=x, 奇数番目=y とみなす
        # (SVGの M x1 y1 x2 y2 ... 形式)
        ys = values[1::2]
        if len(ys) < 2:
            return None

        last_y = ys[-1]
        y_min = min(ys)
        y_max = max(ys)
        if y_max == y_min:
            return None      # 値が縮退、安全策で中央値フォールバック

        # 線形補間: y_min が画面上部(=high) / y_max が画面下部(=low)
        # last_y が y_min に近いほど high に近い、y_max に近いほど low に近い
        fraction = (last_y - y_min) / (y_max - y_min)   # 0.0〜1.0
        current = high - fraction * (high - low)

        # サニティチェック: 値域から大きく外れていたら採用しない
        margin = (high - low) * 0.05
        if not (low - margin <= current <= high + margin):
            return None
        return current
    except Exception:
        return None


def _parse_sekai_q_calls(text: str) -> Dict[str, Dict[str, float]]:
    """
    q(count, low, high, offset, samples, 'svg...') の羅列を解析。

    観察した構造: q() エントリは「ペア構造」
      - 1番目 = 通常市場(金曜終値で固定)
      - 2番目 = サンデー24h CFD/参考値(リアルタイムで動く) ← ユーザーが見たいのはこっち

    各エントリのSVGパス末尾を解析して 'current' 値を抽出。失敗時は None。

    返り値: { "dow": {low, high, current}, "nas100": {...}, "vix": {...} }
    """
    candidates: Dict[str, List[Dict[str, float]]] = {}

    matches = list(_Q_PATTERN.finditer(text))
    for i, m in enumerate(matches):
        try:
            low = float(m.group(2))
            high = float(m.group(3))
            offset = int(m.group(4))
        except (ValueError, TypeError):
            continue
        if not (low > 0 and high > 0 and high >= low):
            continue

        # この q() 引数のあとから、次の q() 開始位置までを SVG 領域とみなす
        svg_start = m.end()
        if i + 1 < len(matches):
            svg_end = matches[i + 1].start()
        else:
            svg_end = min(svg_start + 4000, len(text))
        svg_segment = text[svg_start:svg_end]

        # SVGパス末尾から現在値を逆算
        current = _extract_current_from_svg(svg_segment, low, high)

        for name, lo, hi in _INSTRUMENT_RANGES:
            if lo <= low and high <= hi:
                candidates.setdefault(name, []).append({
                    "low": low,
                    "high": high,
                    "offset": offset,
                    "current": current,
                })
                break

    out: Dict[str, Dict[str, float]] = {}
    for name, cands in candidates.items():
        # 候補が2つ以上あれば2番目(サンデー版/24h版)を採用
        chosen = cands[1] if len(cands) >= 2 else cands[0]
        entry = {"low": chosen["low"], "high": chosen["high"]}
        if chosen.get("current") is not None:
            entry["current"] = chosen["current"]
        out[name] = entry

    return out


def _fetch_sekai_indices() -> Dict[str, Any]:
    """sekai-kabuka.com からダウ/NAS100/VIX を取得。常にdictを返し、失敗時は error に理由を入れる"""
    url, url_source = _discover_sekai_data_url()
    if not url:
        return {"data": {}, "error": "URL抽出失敗(HTML/Secrets共に取れず)", "url": None, "url_source": "none"}

    # キャッシュバスターを付与 (CDNキャッシュ回避)
    sep = "&" if "?" in url else "?"
    url_with_bust = f"{url}{sep}_={int(time.time())}"

    r = _safe_get(url_with_bust, headers={
        "Accept": "*/*",
        "Accept-Language": "ja,en-US;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://sekai-kabuka.com/",
        "Sec-Fetch-Dest": "script",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "same-site",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }, timeout=10)
    if not r:
        return {"data": {}, "error": "データURLへのGETが失敗", "url": url, "url_source": url_source}
    # requestsは Content-Encoding: gzip を自動展開する
    try:
        text = r.content.decode("shift_jis", errors="ignore")
    except Exception:
        text = r.text or ""

    parsed = _parse_sekai_q_calls(text)
    if not parsed:
        return {"data": {}, "error": "q()パターンが見つからず", "url": url, "url_source": url_source}
    return {"data": parsed, "error": None, "url": url, "url_source": url_source}


# =====================================================
# (3) Yahoo Finance による再計算
#     - JP225 を JST 15:30 基準で再計算
#     - US100 を ET 16:00 基準で再計算
#     - USDJPY を ET 16:00 基準で再計算
# =====================================================
def _fetch_yahoo_minute_chart(symbol: str, range_str: str = "5d", interval: str = "5m") -> Optional[List[Tuple[int, float]]]:
    """
    Yahoo Finance の chart API から (unix_ts, close) のリストを返す
    range = 1d/2d/5d/1mo... interval = 1m/2m/5m/15m...
    """
    url = f"{YAHOO_CHART_BASE}{symbol}?interval={interval}&range={range_str}"
    r = _safe_get(url, headers={
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://finance.yahoo.com/",
    }, timeout=8)
    if not r:
        return None
    try:
        j = r.json()
        result = j.get("chart", {}).get("result")
        if not result:
            return None
        node = result[0]
        ts_list = node.get("timestamp") or []
        ind = node.get("indicators", {}).get("quote", [{}])[0]
        close_list = ind.get("close") or []
        out: List[Tuple[int, float]] = []
        for ts, c in zip(ts_list, close_list):
            if c is None:
                continue
            try:
                out.append((int(ts), float(c)))
            except (TypeError, ValueError):
                continue
        return out if out else None
    except Exception:
        return None


def _find_anchor_close(bars: List[Tuple[int, float]],
                       anchor_hour: int,
                       anchor_minute: int,
                       anchor_tz: timezone) -> Optional[Tuple[float, datetime]]:
    """
    bars の中から「直近の anchor_hour:anchor_minute (anchor_tz基準)」に最も近いバーを探す
    そのバーの close を「再計算基準値」として返す
    """
    if not bars:
        return None
    now_utc = datetime.now(timezone.utc)
    # 直近の (今日 or 昨日) の anchor 時刻 を計算
    candidates = []
    for day_offset in (0, 1, 2):
        d = (now_utc.astimezone(anchor_tz) - timedelta(days=day_offset)).replace(
            hour=anchor_hour, minute=anchor_minute, second=0, microsecond=0
        )
        if d <= now_utc.astimezone(anchor_tz):
            candidates.append(d.astimezone(timezone.utc))
    if not candidates:
        return None
    target_utc = max(candidates)   # 直近の(過去の)アンカー時刻

    # bars 内で target_utc に最も近い (かつ target_utc 以下) を選ぶ
    best: Optional[Tuple[int, float]] = None
    target_ts = int(target_utc.timestamp())
    for ts, c in bars:
        if ts <= target_ts:
            if best is None or ts > best[0]:
                best = (ts, c)
    if best is None:
        # アンカー以前のバーが無ければ最古を使う
        best = bars[0]
    return (best[1], datetime.fromtimestamp(best[0], tz=anchor_tz))


def _compute_recalc(symbol: str,
                    anchor_hour: int,
                    anchor_minute: int,
                    anchor_tz_name: str = "JST") -> Optional[Dict[str, Any]]:
    """
    指定銘柄について、指定アンカー時刻(直近の過去回)を基準とした
    変化額・変化率を再計算する
    """
    if anchor_tz_name == "JST":
        anchor_tz = JST
    elif anchor_tz_name == "ET":
        # ET: 11月〜3月はEST(-5)、3月〜11月はEDT(-4)
        # 米国DSTを概算判定 (3月第2日曜〜11月第1日曜=EDT)
        now = datetime.now(timezone.utc)
        y = now.year
        # DST start: 3月第2日曜 02:00 ET
        mar1 = datetime(y, 3, 1, tzinfo=timezone.utc)
        dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)  # 3月第2日曜
        # DST end: 11月第1日曜 02:00 ET
        nov1 = datetime(y, 11, 1, tzinfo=timezone.utc)
        dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
        is_dst = dst_start <= now < dst_end
        anchor_tz = timezone(timedelta(hours=-4 if is_dst else -5))
    else:
        anchor_tz = JST

    bars = _fetch_yahoo_minute_chart(symbol, range_str="5d", interval="5m")
    if not bars or len(bars) < 2:
        return None

    anchor = _find_anchor_close(bars, anchor_hour, anchor_minute, anchor_tz)
    if not anchor:
        return None
    anchor_close, anchor_dt = anchor
    if anchor_close <= 0:
        return None

    current_close = bars[-1][1]
    if current_close <= 0:
        return None

    change = current_close - anchor_close
    change_pct = (change / anchor_close) * 100.0
    return {
        "current": current_close,
        "anchor": anchor_close,
        "anchor_time": anchor_dt.strftime("%m/%d %H:%M %Z"),
        "change": change,
        "change_pct": change_pct,
    }


# =====================================================
# 公開API: fetch_synthetic_fx
# =====================================================
def fetch_synthetic_fx() -> Dict[str, Any]:
    """
    app.py から呼ばれるエントリポイント。
    60秒キャッシュ。失敗時は空dictまたは部分dictを返す。
    例外は外に投げない。
    """
    try:
        now_ts = time.time()
        with _SYN_CACHE_LOCK:
            cached = _SYN_CACHE.get("data")
            cached_ts = _SYN_CACHE.get("ts", 0)
            if cached and (now_ts - cached_ts) < _CACHE_TTL_SEC:
                return cached

        is_we = _is_weekend_jst()

        # 土日のみ: 合成USDJPY と sekai-kabuka
        synth_usdjpy = None
        sekai_result: Dict[str, Any] = {"data": {}, "error": None, "url": None}
        if is_we:
            try:
                synth_usdjpy = _compute_synth_usdjpy()
            except Exception as e:
                synth_usdjpy = {"value": None, "error": f"例外: {type(e).__name__}"}
            try:
                sekai_result = _fetch_sekai_indices()
            except Exception as e:
                sekai_result = {"data": {}, "error": f"例外: {type(e).__name__}", "url": None}

        # 平日も土日も: Yahoo経由の再計算
        recalc: Dict[str, Optional[dict]] = {"jp225": None, "us100": None, "usdjpy": None}
        try:
            recalc["jp225"] = _compute_recalc("^N225", 15, 30, "JST")
        except Exception:
            pass
        try:
            recalc["us100"] = _compute_recalc("^NDX", 16, 0, "ET")
        except Exception:
            pass
        try:
            recalc["usdjpy"] = _compute_recalc("JPY=X", 16, 0, "ET")
        except Exception:
            pass

        # ★ 土日の前日比%計算用: 金曜終値を別取得 (^DJI, ^NDX, ^VIX, JPY=X)
        # recalcのanchorは「直近の(JST15:30 or ET16:00)時刻のバー」を取るので
        # 土日にはそれが金曜終値になり、これを「金曜終値」として使う。
        # NDX/USDJPYはrecalcに既に入っているので流用、Dow/VIXは新規取得。
        prev_closes: Dict[str, Optional[float]] = {
            "dow":    None,
            "nas100": None,
            "vix":    None,
            "usdjpy": None,
        }
        if is_we:
            try:
                _dji = _compute_recalc("^DJI", 16, 0, "ET")
                if _dji and _dji.get("anchor"):
                    prev_closes["dow"] = float(_dji["anchor"])
            except Exception:
                pass
            try:
                _vix = _compute_recalc("^VIX", 16, 0, "ET")
                if _vix and _vix.get("anchor"):
                    prev_closes["vix"] = float(_vix["anchor"])
            except Exception:
                pass
            # NAS100とUSDJPYは既に取得済みのrecalcから流用
            try:
                if recalc.get("us100") and recalc["us100"].get("anchor"):
                    prev_closes["nas100"] = float(recalc["us100"]["anchor"])
            except Exception:
                pass
            try:
                if recalc.get("usdjpy") and recalc["usdjpy"].get("anchor"):
                    prev_closes["usdjpy"] = float(recalc["usdjpy"]["anchor"])
            except Exception:
                pass

        # 現在JST時刻
        nowjst = _now_jst()
        data = {
            "is_weekend": is_we,
            "synth_usdjpy": synth_usdjpy,
            "sekai": sekai_result.get("data", {}),
            "sekai_error": sekai_result.get("error"),
            "sekai_url": sekai_result.get("url"),
            "sekai_url_source": sekai_result.get("url_source", "n/a"),
            "recalc": recalc,
            "prev_closes": prev_closes,
            "as_of": nowjst.strftime("%H:%M:%S JST"),
            "now_jst": nowjst.strftime("%Y/%m/%d (%a) %H:%M:%S JST"),
        }
        with _SYN_CACHE_LOCK:
            _SYN_CACHE["data"] = data
            _SYN_CACHE["ts"] = now_ts
        return data
    except Exception:
        return {}


# =====================================================
# 公開API: render_synthetic_fx
# =====================================================
def render_synthetic_fx(data: Dict[str, Any]) -> None:
    """
    既存3チャート(TradingView)の真上に、再計算値と土日合成値を表示する。
    既存の render_items() には一切触らない。
    """
    if not data:
        return
    is_we = data.get("is_weekend", False)
    recalc = data.get("recalc") or {}
    synth = data.get("synth_usdjpy")
    sekai = data.get("sekai") or {}
    sekai_error = data.get("sekai_error")
    now_jst = data.get("now_jst", "")

    # ================================================
    # 1段目: 再計算カード (常時表示・既存3チャートと同じ並び)
    # ================================================
    def _recalc_card(label: str, info: Optional[dict], unit: str = "") -> str:
        if not info:
            return (
                f'<div class="syn-card syn-dim">'
                f'<div class="syn-label">{label}</div>'
                f'<div class="syn-value">--</div>'
                f'<div class="syn-sub">取得失敗</div></div>'
            )
        cur = info.get("current") or 0
        chg = info.get("change") or 0
        pct = info.get("change_pct") or 0
        anchor = info.get("anchor") or 0
        atime = info.get("anchor_time") or ""
        color = "#1aaa55" if chg >= 0 else "#d32f2f"
        sign = "+" if chg >= 0 else ""
        cur_fmt = f"{cur:,.3f}" if abs(cur) < 1000 else f"{cur:,.2f}"
        chg_fmt = f"{sign}{chg:,.2f}"
        pct_fmt = f"{sign}{pct:.2f}%"
        # 市場休場中なら強調表示
        is_closed = abs(chg) < 0.001
        closed_badge = '<span class="syn-closed">市場休場中</span>' if is_closed else ''
        return (
            f'<div class="syn-card">'
            f'<div class="syn-label">{label} {closed_badge}</div>'
            f'<div class="syn-value" style="color:{color};">{cur_fmt}{unit}</div>'
            f'<div class="syn-chg" style="color:{color};">{chg_fmt} ({pct_fmt})</div>'
            f'<div class="syn-sub">基準 {anchor:,.2f} @ {atime}</div>'
            f'</div>'
        )

    recalc_row = (
        _recalc_card("🇯🇵 JP225 (15:30 JST起点)", recalc.get("jp225"))
        + _recalc_card("🇺🇸 US100 (16:00 ET起点)", recalc.get("us100"))
        + _recalc_card("💴 USDJPY (16:00 ET起点)", recalc.get("usdjpy"))
    )

    # ================================================
    # 2段目: 土日カード (is_weekendがTrueの時のみ表示・失敗時は理由表示)
    # 並び順: ① ダウ → ② NASDAQ100 → ③ ドル円(合成) → ④ VIX
    # ================================================
    weekend_row = ""
    if is_we:
        prev_closes = data.get("prev_closes") or {}

        def _chg_html(current_val: float, prev_close: Optional[float]) -> str:
            """金曜終値からの変化金額と変化率をHTMLで返す。出所表記は付けない。"""
            if not prev_close or prev_close <= 0 or not current_val:
                return ''
            chg = current_val - prev_close
            pct = (chg / prev_close) * 100.0
            color = "#1aaa55" if chg >= 0 else "#d32f2f"
            sign = "+" if chg >= 0 else ""
            return (
                f'<div class="syn-chg" style="color:{color};">'
                f'{sign}{chg:,.2f} ({sign}{pct:.2f}%)'
                f'</div>'
            )

        # ── ① 土日ダウ
        if sekai and "dow" in sekai:
            lo = sekai["dow"]["low"]
            hi = sekai["dow"]["high"]
            # SVGから抽出した現在値があればそれを、なければレンジ中央値
            cur = sekai["dow"].get("current")
            display_val = cur if cur is not None else (lo + hi) / 2
            chg_html = _chg_html(display_val, prev_closes.get("dow"))
            weekend_row += (
                f'<div class="syn-card syn-weekend">'
                f'<div class="syn-label">🇺🇸 土日ダウ</div>'
                f'<div class="syn-value">{display_val:,.2f}</div>'
                f'{chg_html}'
                f'</div>'
            )

        # ── ② 土日NASDAQ100
        if sekai and "nas100" in sekai:
            lo = sekai["nas100"]["low"]
            hi = sekai["nas100"]["high"]
            cur = sekai["nas100"].get("current")
            display_val = cur if cur is not None else (lo + hi) / 2
            chg_html = _chg_html(display_val, prev_closes.get("nas100"))
            weekend_row += (
                f'<div class="syn-card syn-weekend">'
                f'<div class="syn-label">🇺🇸 土日NASDAQ100</div>'
                f'<div class="syn-value">{display_val:,.2f}</div>'
                f'{chg_html}'
                f'</div>'
            )

        # ── ③ 土日ドル円
        if synth and synth.get("value"):
            v = synth["value"]
            chg_html = _chg_html(v, prev_closes.get("usdjpy"))
            weekend_row += (
                f'<div class="syn-card syn-weekend">'
                f'<div class="syn-label">💴 土日ドル円</div>'
                f'<div class="syn-value">{v:,.3f}</div>'
                f'{chg_html}'
                f'</div>'
            )
        elif synth and synth.get("error"):
            weekend_row += (
                f'<div class="syn-card syn-weekend syn-dim">'
                f'<div class="syn-label">💴 土日ドル円</div>'
                f'<div class="syn-value">--</div>'
                f'</div>'
            )

        # ── ④ 土日VIX
        if sekai and "vix" in sekai:
            lo = sekai["vix"]["low"]
            hi = sekai["vix"]["high"]
            cur = sekai["vix"].get("current")
            display_val = cur if cur is not None else (lo + hi) / 2
            chg_html = _chg_html(display_val, prev_closes.get("vix"))
            weekend_row += (
                f'<div class="syn-card syn-weekend">'
                f'<div class="syn-label">😱 土日VIX</div>'
                f'<div class="syn-value">{display_val:,.2f}</div>'
                f'{chg_html}'
                f'</div>'
            )

        # sekai 全滅時のエラーカード (出所文言なし)
        if (not sekai) and sekai_error:
            weekend_row += (
                f'<div class="syn-card syn-weekend syn-dim">'
                f'<div class="syn-label">🌐 株価指数</div>'
                f'<div class="syn-value">--</div>'
                f'</div>'
            )

        if not weekend_row:
            weekend_row = (
                f'<div class="syn-card syn-weekend syn-dim">'
                f'<div class="syn-label">土日機能</div>'
                f'<div class="syn-value">--</div>'
                f'<div class="syn-sub">データ取得失敗・後ほど再試行</div>'
                f'</div>'
            )
    # 平日は土日行は出さない

    # ================================================
    # 出力
    # ================================================
    sekai_url_source = data.get("sekai_url_source", "n/a")
    # データソースのバッジ (色分けで状態が一目で分かる)
    src_color = {
        "auto":   "#1565c0",  # 青: GitHub Actionsで自動取得した最新URL (最良)
        "html":   "#2e7d32",  # 緑: ページHTMLから直接抽出 (次に良い)
        "manual": "#ef6c00",  # オレンジ: 手動URL使用中(古い可能性)
        "cache":  "#c62828",  # 赤: 前回URL再利用(かなり古い可能性)
        "none":   "#9e9e9e",  # グレー: 未取得
        "n/a":    "#9e9e9e",
    }.get(sekai_url_source, "#9e9e9e")
    src_label = {
        "auto":   "🔄 自動更新",
        "html":   "HTML抽出",
        "manual": "手動URL",
        "cache":  "古い",
        "none":   "未取得",
        "n/a":    "",
    }.get(sekai_url_source, sekai_url_source)
    badge = (
        f'<span style="background:{src_color};color:#fff;font-size:9px;'
        f'padding:1px 6px;border-radius:3px;margin-left:6px;font-weight:600;">'
        f'{src_label}</span>'
    ) if src_label else ""

    weekend_html = (
        f'<div class="syn-wrap syn-weekend-wrap">'
        f'<div class="syn-weekend-title">📅 土日モード ({now_jst}){badge}</div>'
        f'<div class="syn-weekend-subtitle">土日に動く為替と株価指数です。参考値です。</div>'
        f'<div class="syn-wrap" style="margin:0;">{weekend_row}</div>'
        f'</div>'
    ) if weekend_row else ""

    html = """
<style>
.syn-wrap{
  display:flex; gap:6px; margin: 4px 0 8px 0;
  flex-wrap: nowrap; align-items: stretch;
}
.syn-card{
  flex: 1 1 0;
  min-width: 0;
  /* 明るめのカード背景にして黒文字を読みやすく */
  background: rgba(255, 240, 200, 0.95);
  border: 1.5px solid rgba(200, 140, 0, 0.7);
  border-radius: 8px;
  padding: 6px 10px;
  line-height: 1.15;
  overflow: hidden;
  color: #111;
}
.syn-card.syn-weekend{
  border-color: rgba(46, 125, 50, 0.7);
  background: rgba(200, 240, 210, 0.95);
}
.syn-card.syn-dim{ opacity: 0.6; }
.syn-label{
  font-size: 13px;
  font-weight: 800;
  color: #111;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
    "Hiragino Kaku Gothic ProN", "Yu Gothic UI", "Meiryo", sans-serif;
  letter-spacing: 0.2px;
  line-height: 1.25;
  /* 見切れ防止: 折り返しを許可してすべて表示 */
  white-space: normal;
  word-break: keep-all;
  overflow: visible;
  text-overflow: clip;
  margin-bottom: 2px;
}
/* 土日カードは緑系背景なので黒文字でも十分読める */
.syn-card.syn-weekend .syn-label{ color: #0a3d1f; }
.syn-value{
  font-size: 22px;
  font-weight: 800;
  letter-spacing: 0.5px;
  color: #111;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
    "Hiragino Kaku Gothic ProN", "Yu Gothic UI", "Meiryo", sans-serif;
}
.syn-chg{ font-size: 13px; font-weight: 700; }
.syn-sub{ font-size: 10px; color: #555; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
.syn-sub2{ font-size: 10px; color: #444; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
.syn-closed{
  display: inline-block;
  font-size: 9px;
  background: #888; color: #fff;
  padding: 1px 4px; border-radius: 3px;
  margin-left: 4px; font-weight: 600;
}
.syn-weekend-wrap{
  display: block;
  border: 1px dashed rgba(76, 175, 80, 0.5);
  border-radius: 8px;
  padding: 6px;
  margin: 8px 0;
  background: rgba(76, 175, 80, 0.03);
}
.syn-weekend-title{
  font-size: 13px;
  font-weight: 800;
  color: #0a3d1f;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
    "Hiragino Kaku Gothic ProN", "Yu Gothic UI", "Meiryo", sans-serif;
  letter-spacing: 0.2px;
  margin-bottom: 2px;
}
.syn-weekend-subtitle{
  font-size: 11px;
  font-weight: 600;
  color: #1b5e20;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
    "Hiragino Kaku Gothic ProN", "Yu Gothic UI", "Meiryo", sans-serif;
  margin-bottom: 6px;
  letter-spacing: 0.1px;
}
</style>
__WEEKEND__
<div class="syn-wrap">__RECALC__</div>
""".replace("__RECALC__", recalc_row).replace("__WEEKEND__", weekend_html)

    st.markdown(html, unsafe_allow_html=True)
