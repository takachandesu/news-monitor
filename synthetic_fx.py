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
def _discover_sekai_data_url() -> Optional[str]:
    """
    1) Streamlit Secrets の manual_url があればそれを使う (推奨運用)
    2) HTMLを取得しURLを抽出（JS生成だと失敗する可能性が高い）
    3) 前回成功URLをフォールバック
    """
    global _LAST_SEKAI_DATA_URL

    # 1) 手動上書き (推奨)
    try:
        manual = st.secrets.get("sekai_kabuka", {}).get("manual_url", "")
        if manual and isinstance(manual, str) and manual.strip():
            url = manual.strip()
            if url.startswith("//"):
                url = "https:" + url
            return url
    except Exception:
        pass

    # 2) HTML を取得して正規表現抽出 (複数エンコーディングで試す)
    r = _safe_get(SEKAI_HOME_URL, headers={
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ja,en-US;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://sekai-kabuka.com/",
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
                return url

    # 3) 前回成功URL
    return _LAST_SEKAI_DATA_URL


# q( count, low, high, offset, samples, 'svg文字列' ) を抽出する正規表現
_Q_PATTERN = re.compile(
    r'q\s*\(\s*(\d+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,'
)


def _parse_sekai_q_calls(text: str) -> Dict[str, Dict[str, float]]:
    """
    q(count, low, high, offset, samples, 'svg...') の羅列を解析。
    - offset=0 の行 = 今日のデータ
    - offset!=0 の行 = 前日比較用(無視)
    返り値: { "dow": {low, high}, "nas100": {...}, "vix": {...} }
    """
    out: Dict[str, Dict[str, float]] = {}
    seen: set = set()

    for m in _Q_PATTERN.finditer(text):
        try:
            low = float(m.group(2))
            high = float(m.group(3))
            offset = int(m.group(4))
        except (ValueError, TypeError):
            continue
        if offset != 0:        # 今日のデータのみ採用
            continue
        if not (low > 0 and high > 0 and high >= low):
            continue

        # 銘柄判定（最初にマッチしたものを採用）
        for name, lo, hi in _INSTRUMENT_RANGES:
            if name in seen:
                continue
            if lo <= low and high <= hi:
                out[name] = {"low": low, "high": high}
                seen.add(name)
                break

        # 全部見つかったら早期終了
        if len(seen) >= len(_INSTRUMENT_RANGES):
            break

    return out


def _fetch_sekai_indices() -> Dict[str, Any]:
    """sekai-kabuka.com からダウ/NAS100/VIX を取得。常にdictを返し、失敗時は status に理由を入れる"""
    url = _discover_sekai_data_url()
    if not url:
        return {"data": {}, "error": "URL抽出失敗(HTML/Secrets共に取れず)", "url": None}
    r = _safe_get(url, headers={
        "Accept": "*/*",
        "Accept-Language": "ja,en-US;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://sekai-kabuka.com/",
        "Sec-Fetch-Dest": "script",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "same-site",
    }, timeout=10)
    if not r:
        return {"data": {}, "error": "データURLへのGETが失敗", "url": url}
    # requestsは Content-Encoding: gzip を自動展開する
    try:
        text = r.content.decode("shift_jis", errors="ignore")
    except Exception:
        text = r.text or ""

    parsed = _parse_sekai_q_calls(text)
    if not parsed:
        return {"data": {}, "error": "q()パターンが見つからず", "url": url}
    return {"data": parsed, "error": None, "url": url}


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

        # 現在JST時刻
        nowjst = _now_jst()
        data = {
            "is_weekend": is_we,
            "synth_usdjpy": synth_usdjpy,
            "sekai": sekai_result.get("data", {}),
            "sekai_error": sekai_result.get("error"),
            "sekai_url": sekai_result.get("url"),
            "recalc": recalc,
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
    # ================================================
    weekend_row = ""
    if is_we:
        # 合成USDJPY
        if synth and synth.get("value"):
            v = synth["value"]
            weekend_row += (
                f'<div class="syn-card syn-weekend">'
                f'<div class="syn-label">🟡 合成USD/JPY (土日)</div>'
                f'<div class="syn-value">{v:,.3f}</div>'
                f'<div class="syn-sub">bitFlyer BTC/JPY ÷ Binance BTC/USDT</div>'
                f'</div>'
            )
        elif synth and synth.get("error"):
            weekend_row += (
                f'<div class="syn-card syn-weekend syn-dim">'
                f'<div class="syn-label">🟡 合成USD/JPY (土日)</div>'
                f'<div class="syn-value">--</div>'
                f'<div class="syn-sub">取得失敗: {synth.get("error","")}</div>'
                f'</div>'
            )

        # sekai-kabuka 由来 (ダウ/NAS/VIX)
        labels = {
            "dow":    ("🇺🇸 ダウ 24h CFD (土日)", ""),
            "nas100": ("🇺🇸 NASDAQ100 24h (土日)", ""),
            "vix":    ("😱 VIX (土日参考値)", ""),
        }
        if sekai:
            for key, (lbl, unit) in labels.items():
                if key in sekai:
                    lo = sekai[key]["low"]
                    hi = sekai[key]["high"]
                    weekend_row += (
                        f'<div class="syn-card syn-weekend">'
                        f'<div class="syn-label">{lbl}</div>'
                        f'<div class="syn-value">{(lo+hi)/2:,.2f}{unit}</div>'
                        f'<div class="syn-sub">本日レンジ {lo:,.2f} 〜 {hi:,.2f}</div>'
                        f'</div>'
                    )
        if not sekai and sekai_error:
            weekend_row += (
                f'<div class="syn-card syn-weekend syn-dim">'
                f'<div class="syn-label">🌐 sekai-kabuka (ダウ/NAS/VIX)</div>'
                f'<div class="syn-value">--</div>'
                f'<div class="syn-sub">取得失敗: {sekai_error}</div>'
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
    weekend_html = (
        f'<div class="syn-wrap syn-weekend-wrap">'
        f'<div class="syn-weekend-title">📅 土日モード ({now_jst})</div>'
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
  background: rgba(255, 255, 255, 0.02);
  border: 1px solid rgba(245, 180, 0, 0.45);
  border-radius: 8px;
  padding: 6px 10px;
  line-height: 1.15;
  overflow: hidden;
}
.syn-card.syn-weekend{
  border-color: rgba(76, 175, 80, 0.55);
  background: rgba(76, 175, 80, 0.06);
}
.syn-card.syn-dim{ opacity: 0.6; }
.syn-label{
  font-size: 11px;
  font-weight: 700;
  color: #f5b400;
  text-shadow: 0 0 2px rgba(0,0,0,0.5);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.syn-value{
  font-size: 22px;
  font-weight: 800;
  letter-spacing: 0.5px;
}
.syn-chg{ font-size: 13px; font-weight: 700; }
.syn-sub{ font-size: 10px; opacity: 0.65; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
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
  font-size: 11px;
  font-weight: 700;
  color: #4caf50;
  margin-bottom: 4px;
}
</style>
__WEEKEND__
<div class="syn-wrap">__RECALC__</div>
""".replace("__RECALC__", recalc_row).replace("__WEEKEND__", weekend_html)

    st.markdown(html, unsafe_allow_html=True)
