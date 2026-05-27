# app.py
# News Headline Monitor
# ✅ 条件
# 1) 別タブ閲覧中でもニュース取得は更新され続ける（バックグラウンド収集）
# 2) タブに戻った瞬間に最新が表示される（visibilitychangeで自動リロード）
# 3) デフォルト：更新間隔30秒 / 表示120本

import re
import time
import json
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import requests
import feedparser
import streamlit as st
import streamlit.components.v1 as components

# ★追加: 合成FX機能（独立モジュール、失敗しても既存機能には影響なし）
# import失敗時の理由は _SYNTHETIC_FX_IMPORT_ERROR に保持し、本体描画時に小さく表示する
_SYNTHETIC_FX_IMPORT_ERROR = None
_SYNTHETIC_FX_IMPORT_TRACEBACK = None
try:
    from synthetic_fx import fetch_synthetic_fx, render_synthetic_fx
    _HAS_SYNTHETIC_FX = True
except Exception as _e:
    _HAS_SYNTHETIC_FX = False
    _SYNTHETIC_FX_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"
    import traceback as _tb
    _SYNTHETIC_FX_IMPORT_TRACEBACK = _tb.format_exc()
    def fetch_synthetic_fx():
        return {}
    def render_synthetic_fx(_data):
        return

# -----------------------------
# Page / Global CSS
# -----------------------------
st.set_page_config(page_title="News Headline Monitor", layout="wide")


# -----------------------------
# Password gate (disabled: public access)
# -----------------------------
def _check_password() -> bool:
    """パスワード保護無効化（誰でも閲覧可）"""
    return True


_check_password()


BASE_CSS = """
<style>
/* 全体余白 */
.block-container {
  padding-top: 1.2rem !important;
  padding-bottom: 1.5rem !important;
  max-width: 1500px;
}
h1, h2, h3 { margin-bottom: 0.35rem !important; }
p { margin-bottom: 0.35rem !important; }
[data-testid="stSidebar"] .block-container { padding-top: 1rem !important; }

/* タブ（radio）を横スクロール可能に */
div[data-testid="stRadio"] > div[role="radiogroup"]{
  flex-wrap: nowrap !important;
  overflow-x: auto !important;
  overflow-y: hidden !important;
  white-space: nowrap !important;
  padding-bottom: 10px !important;
}
div[data-testid="stRadio"] > div[role="radiogroup"] label {
  display: inline-flex !important;
  margin-right: 10px !important;
}

/* 見出しリスト行 */
.news-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin: 1px 0;
}
.news-open a{
  display: inline-block;
  padding: 1px 8px;
  border: 1px solid rgba(49,51,63,.25);
  border-radius: 8px;
  text-decoration: none;
  font-size: 12px;
  white-space: nowrap;
}
.news-meta{
  opacity: 0.65;
  font-size: 12px;
  margin-left: 6px;
  white-space: nowrap;
}
.news-title{
  line-height: 1.15;
}
hr {
  margin: 6px 0 !important;
}
</style>
"""
st.markdown(BASE_CSS, unsafe_allow_html=True)

# -----------------------------
# Helpers
# -----------------------------
def now_ms() -> int:
    return int(time.time() * 1000)


def safe_get(url: str, timeout: int = 20, headers: Optional[dict] = None) -> requests.Response:
    h = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/javascript, application/javascript, */*;q=0.9",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://nikkei225jp.com/news/",
        "Connection": "keep-alive",
    }
    if headers:
        h.update(headers)
    r = requests.get(url, timeout=timeout, headers=h)
    r.raise_for_status()
    return r


def normalize_dt(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# JST (UTC+9) タイムゾーン定数。見出しの時刻表示などで使用。
JST = timezone(timedelta(hours=9))


def fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    try:
        return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
    except Exception:
        return ""


def dedupe(items: List[Dict]) -> List[Dict]:
    seen = set()
    out = []
    for it in items:
        key = it.get("url") or it.get("title")
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def sort_items(items: List[Dict]) -> List[Dict]:
    # publishedがあるものを優先して新しい順。無いものは後ろ
    def key(it):
        dt = it.get("published")
        if isinstance(dt, datetime):
            return (0, dt.timestamp())
        return (1, 0)
    return list(sorted(items, key=key, reverse=True))


def sort_items_by_effective_time_desc(items: List[Dict]) -> List[Dict]:
    """
    All 用： effective_time = published があればそれ / なければ first_seen
    で新しい順に並べる。どちらも無い場合は最後。
    """
    def eff_dt(it: Dict) -> Optional[datetime]:
        dt = it.get("published")
        if isinstance(dt, datetime):
            return dt
        dt2 = it.get("first_seen")
        if isinstance(dt2, datetime):
            return dt2
        return None

    def key(it: Dict):
        dt = eff_dt(it)
        if isinstance(dt, datetime):
            return (1, dt.timestamp())
        return (0, 0)

    return list(sorted(items, key=key, reverse=True))


def is_probably_title(s: str) -> bool:
    if not s:
        return False
    s = s.strip()
    if len(s) < 6:
        return False
    if re.fullmatch(r"[0-9]+", s):
        return False
    return True


def filter_nsj_star_only(items: List[Dict]) -> List[Dict]:
    """
    日本証券新聞：タイトル先頭が「☆」のものだけ残す
    """
    out = []
    for it in items:
        t = (it.get("title") or "").strip()
        if t.startswith("☆"):
            out.append(it)
    return out


def filter_nikkei_exclude_jinji(items: List[Dict]) -> List[Dict]:
    """
    日経：タイトル先頭が「人事、」のものを除外
    """
    out = []
    for it in items:
        t = (it.get("title") or "").strip()
        if t.startswith("人事、"):
            continue
        out.append(it)
    return out


def item_key(it: Dict) -> str:
    return (it.get("url") or it.get("title") or "").strip()

# -----------------------------
# Fetchers (RSS / Google News / NSJ / nikkei225jp)
# -----------------------------
def fetch_rss_feed(url: str, source_name: str) -> List[Dict]:
    d = feedparser.parse(url)
    items = []
    for e in d.entries:
        title = getattr(e, "title", "").strip()
        link = getattr(e, "link", "").strip()
        published = None
        if getattr(e, "published_parsed", None):
            try:
                published = datetime.fromtimestamp(time.mktime(e.published_parsed), tz=timezone.utc)
            except Exception:
                published = None
        if title and link:
            items.append({"source": source_name, "title": title, "url": link, "published": normalize_dt(published)})
    return dedupe(items)


def google_news_rss(query: str, hl: str, gl: str, ceid: str) -> str:
    from urllib.parse import quote_plus
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl={hl}&gl={gl}&ceid={ceid}"


def fetch_google_news(query: str, source_name: str, hl="ja", gl="JP", ceid="JP:ja") -> List[Dict]:
    url = google_news_rss(query=query, hl=hl, gl=gl, ceid=ceid)
    return fetch_rss_feed(url, source_name=source_name)


def fetch_nsj_sokuhou(url: str, source_name: str) -> List[Dict]:
    """
    nsjournal.jp/category/nsj_short_live/sokuhou/ をスクレイピング。
    「Free」ラベルが付いた記事のみ取得。
    """
    from urllib.parse import urljoin, urlparse

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Referer": "https://www.nsjournal.jp/",
        "Cache-Control": "no-cache",
    }

    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()

    base_domain = urlparse(url).netloc

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, "html.parser")

    items: List[Dict] = []
    seen: set = set()

    def is_free_article(container) -> bool:
        """
        記事コンテナ内に「Free」バッジ（オレンジ角リボン）があるか判定。
        nsjournal.jp は記事左上に <span> や <div> で "Free" テキストを持つ。
        """
        # ① クラス名に "free" / "ribbon" / "badge" を含む子要素
        for el in container.find_all(True):
            cls = " ".join(el.get("class", [])).lower()
            if any(k in cls for k in ("free", "ribbon", "badge", "label")):
                txt = el.get_text(strip=True).lower()
                if "free" in txt:
                    return True
        # ② テキストノードとして直接 "Free" が存在する
        for string in container.strings:
            if string.strip().lower() == "free":
                return True
        return False

    def add_item(href: str, title: str) -> None:
        if not href or not title:
            return
        if href.startswith("/"):
            href = urljoin(url, href)
        if base_domain not in href:
            return
        if "/category/" in href or "/tag/" in href or "/author/" in href:
            return
        if href.startswith("#") or "javascript:" in href.lower():
            return
        title = re.sub(r"\s+", " ", title).strip()
        if not is_probably_title(title) or len(title) < 6:
            return
        key = href.split("?")[0]
        if key in seen:
            return
        seen.add(key)
        items.append({"source": source_name, "title": title, "url": key, "published": None})

    # ── ① article タグ単位で Free チェック ───────────────────────
    articles = soup.find_all("article")
    for art in articles:
        if not is_free_article(art):
            continue
        a = art.find("a", href=True)
        if not a:
            continue
        heading = art.find(["h2", "h3", "h4"])
        title_text = (heading or a).get_text(separator=" ", strip=True)
        add_item(a["href"], title_text)

    # ── ② li / div 単位で Free チェック（articleタグがない場合）────
    if not items:
        for container in soup.find_all(["li", "div"], class_=re.compile(r"post|entry|article|item|news", re.I)):
            if not is_free_article(container):
                continue
            a = container.find("a", href=True)
            if not a:
                continue
            heading = container.find(["h2", "h3", "h4"])
            title_text = (heading or a).get_text(separator=" ", strip=True)
            add_item(a["href"], title_text)

    # ── ③ フォールバック: Free テキストの隣接リンク ─────────────────
    if not items:
        for free_el in soup.find_all(string=re.compile(r'\bfree\b', re.IGNORECASE)):
            parent = free_el.parent
            # 親・兄弟要素から a タグを探す
            for el in [parent] + list(parent.find_all_next("a", limit=2)) + list(parent.find_all_previous("a", limit=2)):
                if el.name == "a" and el.get("href"):
                    add_item(el["href"], el.get_text(separator=" ", strip=True))
                    break

    return items[:250]


def fetch_nikkei225jp_news_all1() -> Dict[str, object]:
    """
    nikkei225jp News_ALL1.js を取得して揺れに強くパース
    返り値:
      {
        "items": List[Dict],
        "debug": { fetched_url, status, len, matches, head }
      }
    """
    base = "https://nikkei225jp.com/_data/_nfsWEB/rss/News_ALL1.js"
    candidates = [
        f"{base}?&_={now_ms()}",
        f"{base}?_={now_ms()}",
        base,
    ]

    text = ""
    fetched_url = candidates[0]
    status = None
    last_exc = None

    for u in candidates:
        try:
            rr = safe_get(u, timeout=20)
            fetched_url = u
            status = rr.status_code
            text = rr.text
            last_exc = None
            break
        except Exception as e:
            last_exc = e

    if last_exc is not None:
        raise last_exc

    payloads = re.findall(r"News\[[^\]]+\]\s*=\s*(['\"])(.*?)\1\s*;?", text, flags=re.DOTALL)
    raw_list = [p[1] for p in payloads]

    items: List[Dict] = []
    for raw in raw_list:
        parts = [x.strip() for x in raw.split("__") if x is not None]
        if not parts:
            continue

        published = None
        for token in parts:
            if re.fullmatch(r"\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}", token):
                try:
                    # nikkei225jp.com の News_ALL1.js は JST 時刻を配信している
                    # （例: ザイFX!「NY市場動向(午前10時台)」の元記事配信時刻 5/27 23:25 JST が
                    # データ上 "2026/05/27 23:25" として入っている）。
                    # ここを JST でタグ付けしないと、fmt_dt の JST 変換で +9時間ズレる。
                    published = datetime.strptime(token, "%Y/%m/%d %H:%M").replace(tzinfo=JST)
                except Exception:
                    published = None
                break

        url_idx = None
        for i, token in enumerate(parts):
            if token.startswith("http://") or token.startswith("https://"):
                url_idx = i
                break
        if url_idx is None:
            continue

        url = parts[url_idx].strip()
        if not url.startswith("http"):
            continue

        title = ""
        for j in range(url_idx + 1, min(url_idx + 6, len(parts))):
            cand = parts[j]
            if is_probably_title(cand):
                title = cand
                break
        if not title:
            for j in range(max(0, url_idx - 4), url_idx):
                cand = parts[j]
                if is_probably_title(cand) and not re.fullmatch(r"\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}", cand):
                    title = cand
                    break
        if not title:
            continue

        # 出典: サブ媒体名があればそれだけを表示（"nikkei225jp/" 接頭辞は付けない）。
        #       サブ媒体名が無ければ空欄にする。
        source = ""
        if url_idx - 1 >= 0:
            s = parts[url_idx - 1]
            if s and len(s) <= 30 and "http" not in s.lower() and not re.fullmatch(r"\d+", s):
                source = s

        # ★ Yahoo! 由来の記事は除外（ユーザー指定）
        # source が "Yahoo!" / "Yahoo" / "ヤフー" 等のいずれかで始まる場合スキップ。
        # URL が news.yahoo.co.jp / topics.yahoo.co.jp 等の場合もスキップ（取りこぼし防止）。
        _src_lower = source.lower()
        _url_lower = url.lower()
        if (
            "yahoo" in _src_lower
            or "ヤフー" in source
            or "yahoo.co.jp" in _url_lower
            or "yahoo.com" in _url_lower
        ):
            continue

        items.append({"source": source, "title": title, "url": url, "published": normalize_dt(published)})

    items = sort_items(dedupe(items))
    debug = {
        "fetched_url": fetched_url,
        "status": status,
        "len": len(text),
        "matches": len(raw_list),
        "head": text[:500],
    }
    return {"items": items, "debug": debug}


# ============================================================
# Reuters 英語 / Bloomberg 英語
# feeds.reuters.com / feeds.bloomberg.com は両社とも公式RSS廃止済み
# Google News の英語設定で代替取得
# ============================================================

def fetch_reuters_en() -> List[Dict]:
    items: List[Dict] = []
    queries = [
        ("site:reuters.com markets",    "Reuters EN / Markets"),
        ("site:reuters.com business",   "Reuters EN / Business"),
        ("site:reuters.com technology", "Reuters EN / Technology"),
        ("site:reuters.com world",      "Reuters EN / World"),
    ]
    for query, name in queries:
        try:
            items.extend(
                fetch_google_news(query, name, hl="en-US", gl="US", ceid="US:en")
            )
        except Exception:
            pass
    return dedupe(items)


def fetch_reuters_jp_direct() -> List[Dict]:
    """
    jp.reuters.com から直接ニュースを取得。
    優先順: ① 公式RSS複数 → ② トップページスクレイピング
    """
    SOURCE = "Reuters JP (jp.reuters.com)"
    items: List[Dict] = []

    # ── ① 公式RSSフィード（複数候補を試す） ──────────────────────────
    rss_candidates = [
        "https://jp.reuters.com/rssFeed/topNews/",
        "https://jp.reuters.com/rssFeed/marketsNews/",
        "https://jp.reuters.com/rssFeed/businessNews/",
        "https://jp.reuters.com/rssFeed/worldNews/",
        "https://jp.reuters.com/rssFeed/technologyNews/",
        "https://jp.reuters.com/rss/topNews",
        "https://jp.reuters.com/rss/marketsNews",
        "https://feeds.reuters.com/Reuters/JPWorldNews",
        "https://feeds.reuters.com/Reuters/JPDomesticNews",
        "https://feeds.reuters.com/reuters/JPBusinessNews",
        "https://feeds.reuters.com/reuters/JPTopNews",
    ]
    rss_got = 0
    for rss_url in rss_candidates:
        try:
            d = feedparser.parse(rss_url)
            if not d.entries:
                continue
            for e in d.entries:
                title = getattr(e, "title", "").strip()
                link  = getattr(e, "link",  "").strip()
                published = None
                if getattr(e, "published_parsed", None):
                    try:
                        published = datetime.fromtimestamp(
                            time.mktime(e.published_parsed), tz=timezone.utc
                        )
                    except Exception:
                        pass
                if title and link:
                    items.append({
                        "source":    SOURCE,
                        "title":     title,
                        "url":       link,
                        "published": normalize_dt(published),
                    })
            rss_got += len(d.entries)
        except Exception:
            pass

    if rss_got > 0:
        return dedupe(items)

    # ── ② フォールバック: トップページをスクレイピング ──────────────
    scrape_urls = [
        "https://jp.reuters.com/",
        "https://jp.reuters.com/markets/",
        "https://jp.reuters.com/business/",
        "https://jp.reuters.com/world/",
    ]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Referer": "https://jp.reuters.com/",
    }
    # jp.reuters.com の記事URLパターン
    article_re = re.compile(
        r"https?://jp\.reuters\.com/"
        r"(?:markets|business|world|technology|economy|asia|sustainability)"
        r"/[a-z0-9\-]+/[A-Z0-9\-]+/?$"
    )
    seen_urls: set = set()
    for page_url in scrape_urls:
        try:
            r = requests.get(page_url, headers=headers, timeout=15)
            r.raise_for_status()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("/"):
                    href = "https://jp.reuters.com" + href
                href = href.split("?")[0].split("#")[0]
                if not article_re.match(href):
                    continue
                if href in seen_urls:
                    continue
                seen_urls.add(href)
                title = (
                    a.get("aria-label")
                    or a.get_text(separator=" ", strip=True)
                )
                title = re.sub(r"\s+", " ", title or "").strip()
                if not is_probably_title(title) or len(title) < 10:
                    continue
                items.append({
                    "source":    SOURCE,
                    "title":     title,
                    "url":       href,
                    "published": None,
                })
            time.sleep(0.5)
        except Exception:
            pass

    return dedupe(items)


def fetch_bloomberg_en_all() -> List[Dict]:
    items: List[Dict] = []
    queries = [
        ("site:bloomberg.com markets",    "Bloomberg EN / Markets"),
        ("site:bloomberg.com politics",   "Bloomberg EN / Politics"),
        ("site:bloomberg.com technology", "Bloomberg EN / Technology"),
        ("site:bloomberg.com economy",    "Bloomberg EN / Economy"),
    ]
    for query, name in queries:
        try:
            items.extend(
                fetch_google_news(query, name, hl="en-US", gl="US", ceid="US:en")
            )
        except Exception:
            pass
    return dedupe(items)


# ============================================================
# ★ 追加: 読売新聞（政治・経済・海外） / 産経新聞（経済・政治）
#   Google News RSS 経由で site:指定により各カテゴリの記事を取得。
# ============================================================
def fetch_yomiuri() -> List[Dict]:
    """読売新聞: 政治 / 経済 / 海外 の3カテゴリを Google News 経由で取得。"""
    items: List[Dict] = []
    queries = [
        ("site:yomiuri.co.jp/politics", "読売新聞／政治"),
        ("site:yomiuri.co.jp/economy",  "読売新聞／経済"),
        ("site:yomiuri.co.jp/world",    "読売新聞／海外"),
    ]
    for query, name in queries:
        try:
            items.extend(
                fetch_google_news(query, name, hl="ja", gl="JP", ceid="JP:ja")
            )
        except Exception:
            pass
    return dedupe(items)


def fetch_sankei() -> List[Dict]:
    """産経新聞: 経済 / 政治 の2カテゴリを Google News 経由で取得。"""
    items: List[Dict] = []
    queries = [
        ("site:sankei.com/economy",  "産経新聞／経済"),
        ("site:sankei.com/politics", "産経新聞／政治"),
    ]
    for query, name in queries:
        try:
            items.extend(
                fetch_google_news(query, name, hl="ja", gl="JP", ceid="JP:ja")
            )
        except Exception:
            pass
    return dedupe(items)


# ============================================================
# ★ 追加: TBS NEWS DIG（Bloomberg提携記事一覧）
#   https://newsdig.tbs.co.jp/list/withbloomberg/news
#
#   この一覧ページから記事タイトル＋URL＋(可能なら)公開時刻を取得する。
#   公式RSSが無いためHTMLスクレイピング方式。
# ============================================================
def fetch_tbs_newsdig_bloomberg() -> List[Dict]:
    """
    TBS NEWS DIG の "with Bloomberg" カテゴリ一覧ページから記事を取得。
    """
    SOURCE = "TBS NEWS DIG / Bloomberg"
    list_url = "https://newsdig.tbs.co.jp/list/withbloomberg/news"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://newsdig.tbs.co.jp/",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    }

    items: List[Dict] = []
    seen: set = set()

    try:
        r = requests.get(list_url, headers=headers, timeout=20)
        r.raise_for_status()

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")

        # TBS NEWS DIGの記事URLパターン:
        #   https://newsdig.tbs.co.jp/articles/-/XXXXXXX
        #   https://newsdig.tbs.co.jp/articles/XXXXXXX
        article_re = re.compile(r"^https?://newsdig\.tbs\.co\.jp/articles/[^?#]+")

        def _parse_dt(s: str) -> Optional[datetime]:
            """ISO8601 or よくある日本語日付文字列をパース"""
            if not s:
                return None
            s = s.strip()
            # ISO 8601 (例: 2025-11-10T07:30:00+09:00 / ...Z)
            try:
                iso = s.replace("Z", "+00:00")
                dt = datetime.fromisoformat(iso)
                return normalize_dt(dt)
            except Exception:
                pass
            # 2025/11/10 07:30 形式
            m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})[^\d]+(\d{1,2}):(\d{2})", s)
            if m:
                try:
                    dt = datetime(
                        int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4)), int(m.group(5)),
                        tzinfo=timezone.utc,
                    )
                    return dt
                except Exception:
                    pass
            return None

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/"):
                href = "https://newsdig.tbs.co.jp" + href
            href = href.split("?")[0].split("#")[0]
            if not article_re.match(href):
                continue
            # 一覧/タグページ等を除外
            if "/list/" in href or "/tag/" in href or "/category/" in href:
                continue
            if href in seen:
                continue
            seen.add(href)

            # タイトル取得（aria-label → title属性 → テキスト → 周辺見出し）
            title = (
                a.get("aria-label")
                or a.get("title")
                or a.get_text(separator=" ", strip=True)
            )
            # テキストが短すぎる/空の場合、親要素内の見出しを探す
            if not title or len(title.strip()) < 8:
                parent = a.parent
                for _ in range(3):
                    if not parent:
                        break
                    heading = parent.find(["h1", "h2", "h3", "h4"])
                    if heading:
                        title = heading.get_text(separator=" ", strip=True)
                        break
                    parent = parent.parent

            title = re.sub(r"\s+", " ", title or "").strip()
            if not is_probably_title(title) or len(title) < 8:
                continue

            # 公開時刻を探す: 近傍の <time datetime="..."> タグ
            published: Optional[datetime] = None
            parent = a.parent
            for _ in range(4):
                if not parent:
                    break
                t_tag = parent.find("time")
                if t_tag:
                    dt_val = t_tag.get("datetime") or t_tag.get_text(strip=True)
                    published = _parse_dt(dt_val or "")
                    if published:
                        break
                parent = parent.parent

            items.append({
                "source": SOURCE,
                "title": title,
                "url": href,
                "published": published,
            })
    except Exception:
        pass

    return dedupe(items)


# ============================================================
# ★ 追加: 日経新聞（Cookie認証）
#
# 使い方:
#   1. Chromeで https://www.nikkei.com にログイン
#   2. F12 → Application → Cookies → https://www.nikkei.com
#   3. 下記キーをコピーして nikkei_cookies.json に保存:
#      RNikkeiAuth, RNikkeiUserInfo
#   4. nikkei_cookies.json を app.py と同じフォルダに置く
#
# nikkei_cookies.json の形式:
#   {
#     "RNikkeiAuth":    "xxxxx",
#     "RNikkeiUserInfo": "xxxxx"
#   }
# ============================================================
NIKKEI_COOKIE_FILE = Path(__file__).parent / "nikkei_cookies.json"

NIKKEI_SCRAPE_URLS = [
    ("https://www.nikkei.com/markets/",         "日経／マーケット"),
    ("https://www.nikkei.com/markets/kabu/",    "日経／国内株"),
    ("https://www.nikkei.com/markets/global/",  "日経／海外株"),
    ("https://www.nikkei.com/markets/forex/",   "日経／為替"),
    ("https://www.nikkei.com/economy/",         "日経／経済"),
]

# 記事リンクに含まれるパターン（カテゴリページや広告を除外）
NIKKEI_ARTICLE_RE = re.compile(r"https://www\.nikkei\.com/article/[A-Z0-9\-]+/?")

def _load_nikkei_cookies() -> Optional[Dict[str, str]]:
    if not NIKKEI_COOKIE_FILE.exists():
        return None
    try:
        with open(NIKKEI_COOKIE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data:
            return None
        return data
    except Exception:
        return None

def fetch_nikkei_cookie() -> List[Dict]:
    """
    日経Webをログイン済みCookieでスクレイピングし、
    記事タイトル＋URLの一覧を返す。
    nikkei_cookies.json が無い場合は空リストを返す。
    """
    cookies = _load_nikkei_cookies()
    if not cookies:
        return []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Referer": "https://www.nikkei.com/",
    }

    all_items: List[Dict] = []
    seen_urls: set = set()

    for page_url, source_name in NIKKEI_SCRAPE_URLS:
        try:
            r = requests.get(page_url, cookies=cookies, headers=headers, timeout=20)
            r.raise_for_status()
            html = r.text

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            for a in soup.find_all("a", href=True):
                href = a["href"]
                # 相対URLを絶対URLに
                if href.startswith("/article/"):
                    href = "https://www.nikkei.com" + href
                if not NIKKEI_ARTICLE_RE.match(href.split("?")[0]):
                    continue
                if href in seen_urls:
                    continue
                seen_urls.add(href)

                # タイトル取得（aria-label → テキスト → 親要素のテキスト）
                title = (
                    a.get("aria-label")
                    or a.get_text(separator=" ", strip=True)
                )
                title = re.sub(r"\s+", " ", title).strip()
                if not is_probably_title(title):
                    continue
                # ナビゲーション文字列を除外
                if len(title) < 10:
                    continue

                all_items.append({
                    "source": source_name,
                    "title": title,
                    "url": href.split("?")[0],
                    "published": None,
                })

            time.sleep(1)  # サーバー負荷軽減

        except Exception as e:
            # 取得失敗してもほかのページは続ける
            pass

    return dedupe(all_items)


def nikkei_cookie_status() -> str:
    """サイドバー表示用: Cookieファイルの状態を返す"""
    cookies = _load_nikkei_cookies()
    if cookies is None:
        return "⚠️ nikkei_cookies.json が見つかりません"
    return f"✅ Cookie読込済 ({len(cookies)}キー)"


# ============================================================
# ★ 追加: X（Twitter）ホームタイムライン
#
# 必要なもの: X Developer Portal で取得した認証情報
#   https://developer.twitter.com/en/portal/dashboard
#
# 使い方:
#   1. X Developer Portal でプロジェクト＆アプリを作成
#   2. "Read" 権限を付与してキーを生成
#   3. 下記の内容を x_credentials.json に保存して app.py と同じフォルダへ
#
# x_credentials.json の形式:
#   {
#     "api_key":             "YOUR_API_KEY",
#     "api_secret":          "YOUR_API_KEY_SECRET",
#     "access_token":        "YOUR_ACCESS_TOKEN",
#     "access_token_secret": "YOUR_ACCESS_TOKEN_SECRET",
#     "bearer_token":        "YOUR_BEARER_TOKEN"
#   }
#
# ※ ホームタイムライン取得には OAuth 1.0a ユーザーコンテキストが必要です
#    （Bearer Token のみでは取得できません）
# ============================================================
X_CRED_FILE = Path(__file__).parent / "x_credentials.json"


def _load_x_credentials() -> Optional[Dict[str, str]]:
    if not X_CRED_FILE.exists():
        return None
    try:
        with open(X_CRED_FILE, encoding="utf-8") as f:
            data = json.load(f)
        required = {"api_key", "api_secret", "access_token", "access_token_secret"}
        if not required.issubset(data.keys()):
            return None
        return data
    except Exception:
        return None


def x_credential_status() -> str:
    """サイドバー表示用: X認証情報ファイルの状態を返す"""
    creds = _load_x_credentials()
    if creds is None:
        return "⚠️ x_credentials.json が見つかりません"
    return f"✅ 認証情報読込済"


def fetch_x_home_timeline(max_results: int = 100) -> List[Dict]:
    """
    X API v2 でホームタイムラインを取得して返す。
    tweepy を使用。x_credentials.json が無い場合は空リストを返す。
    """
    creds = _load_x_credentials()
    if not creds:
        return []

    try:
        import tweepy  # type: ignore
    except ImportError:
        return []

    try:
        client = tweepy.Client(
            bearer_token=creds.get("bearer_token"),
            consumer_key=creds["api_key"],
            consumer_secret=creds["api_secret"],
            access_token=creds["access_token"],
            access_token_secret=creds["access_token_secret"],
            wait_on_rate_limit=False,
        )

        # 自分のユーザーIDを取得
        me_resp = client.get_me(user_fields=["id", "username"])
        if not me_resp or not me_resp.data:
            return []
        my_id = me_resp.data.id

        # ホームタイムライン取得（逆時系列）
        resp = client.get_home_timeline(
            max_results=min(max_results, 100),
            tweet_fields=["created_at", "author_id", "text", "lang"],
            expansions=["author_id"],
            user_fields=["username", "name"],
        )

        if not resp or not resp.data:
            return []

        # ユーザー情報マップを作成（author_id → username）
        user_map: Dict[str, str] = {}
        if resp.includes and resp.includes.get("users"):
            for u in resp.includes["users"]:
                user_map[str(u.id)] = u.username

        items: List[Dict] = []
        for tweet in resp.data:
            text = (tweet.text or "").strip()
            if not text:
                continue
            author_id = str(tweet.author_id)
            username = user_map.get(author_id, author_id)
            tweet_url = f"https://x.com/{username}/status/{tweet.id}"
            published = None
            if tweet.created_at:
                try:
                    published = tweet.created_at.replace(tzinfo=timezone.utc) \
                        if tweet.created_at.tzinfo is None else tweet.created_at
                except Exception:
                    published = None

            items.append({
                "source": f"X ホームTL / @{username}",
                "title": text,
                "url": tweet_url,
                "published": published,
            })

        return items

    except Exception:
        return []


# ============================================================
# ★ 追加②: X 4アカウント対応ソース
#
#  @BloombergJapan → bloomberg.co.jp（Google News + 直接スクレイピング）
#  @business       → bloomberg.com（Google News 強化版）
#  @ReutersJapan   → jp.reuters.com（RSS直接 + Google News）  ←既存流用
#  @Reuters        → reuters.com（RSS直接 + Google News）     ←既存流用
# ============================================================

def fetch_bloomberg_japan_enhanced() -> List[Dict]:
    """
    @BloombergJapan 相当: bloomberg.co.jp の記事を多角的に取得。
    ① Google News（日本語）複数クエリ
    ② bloomberg.co.jp トップ/マーケット直接スクレイピング
    """
    items: List[Dict] = []

    # ① Google News（日本語）
    gn_queries = [
        ("site:bloomberg.co.jp",                    "@BloombergJapan / bloomberg.co.jp"),
        ("site:bloomberg.co.jp マーケット",          "@BloombergJapan / マーケット"),
        ("site:bloomberg.co.jp 経済",                "@BloombergJapan / 経済"),
        ("site:bloomberg.co.jp 日本株",              "@BloombergJapan / 日本株"),
        ("site:bloomberg.co.jp 為替",                "@BloombergJapan / 為替"),
    ]
    for q, name in gn_queries:
        try:
            items.extend(fetch_google_news(q, name, hl="ja", gl="JP", ceid="JP:ja"))
        except Exception:
            pass

    # ② bloomberg.co.jp トップ・マーケットを直接スクレイピング
    scrape_targets = [
        ("https://www.bloomberg.co.jp/",              "@BloombergJapan / bloomberg.co.jp"),
        ("https://www.bloomberg.co.jp/markets",        "@BloombergJapan / bloomberg.co.jp/markets"),
        ("https://www.bloomberg.co.jp/economics",      "@BloombergJapan / bloomberg.co.jp/economics"),
    ]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    article_re = re.compile(
        r"https?://(?:www\.)?bloomberg\.co\.jp/(?:news/articles|news/videos)/[A-Za-z0-9\-]+"
    )
    seen: set = set()
    for page_url, src_name in scrape_targets:
        try:
            r = requests.get(page_url, headers=headers, timeout=15)
            r.raise_for_status()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("/"):
                    href = "https://www.bloomberg.co.jp" + href
                href = href.split("?")[0].split("#")[0]
                if not article_re.match(href):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                title = (
                    a.get("aria-label")
                    or a.get_text(separator=" ", strip=True)
                )
                title = re.sub(r"\s+", " ", title or "").strip()
                if not is_probably_title(title) or len(title) < 8:
                    continue
                items.append({
                    "source": src_name,
                    "title": title,
                    "url": href,
                    "published": None,
                })
            time.sleep(0.5)
        except Exception:
            pass

    return dedupe(items)


def fetch_bloomberg_business_enhanced() -> List[Dict]:
    """
    @business（Bloomberg英語）相当: bloomberg.com のグローバル記事を取得。
    Google News の英語クエリを強化（カテゴリ追加）。
    """
    items: List[Dict] = []
    queries = [
        ("site:bloomberg.com markets",      "@business / Markets"),
        ("site:bloomberg.com politics",     "@business / Politics"),
        ("site:bloomberg.com technology",   "@business / Technology"),
        ("site:bloomberg.com economy",      "@business / Economy"),
        ("site:bloomberg.com finance",      "@business / Finance"),
        ("site:bloomberg.com stocks",       "@business / Stocks"),
        ("site:bloomberg.com bonds",        "@business / Bonds"),
        ("site:bloomberg.com currencies",   "@business / Currencies"),
        ("site:bloomberg.com commodities",  "@business / Commodities"),
    ]
    for q, name in queries:
        try:
            items.extend(fetch_google_news(q, name, hl="en-US", gl="US", ceid="US:en"))
        except Exception:
            pass
    return dedupe(items)


def fetch_reuters_direct_enhanced() -> List[Dict]:
    """
    @Reuters（英語）相当: reuters.com の英語記事を直接RSS＋Google Newsで取得。
    """
    items: List[Dict] = []
    SOURCE = "@Reuters / reuters.com"

    # ① 公式RSSフィード
    rss_candidates = [
        "https://feeds.reuters.com/reuters/topNews",
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.reuters.com/reuters/technologyNews",
        "https://feeds.reuters.com/reuters/worldNews",
        "https://feeds.reuters.com/Reuters/worldNews",
        "https://www.reutersagency.com/feed/?taxonomy=best-topics&post_type=best",
    ]
    for rss_url in rss_candidates:
        try:
            d = feedparser.parse(rss_url)
            if not d.entries:
                continue
            for e in d.entries:
                title = getattr(e, "title", "").strip()
                link  = getattr(e, "link",  "").strip()
                published = None
                if getattr(e, "published_parsed", None):
                    try:
                        published = datetime.fromtimestamp(
                            time.mktime(e.published_parsed), tz=timezone.utc
                        )
                    except Exception:
                        pass
                if title and link:
                    items.append({
                        "source": SOURCE,
                        "title": title,
                        "url": link,
                        "published": normalize_dt(published),
                    })
        except Exception:
            pass

    # ② Google News（英語）フォールバック
    gn_queries = [
        ("site:reuters.com markets",    "@Reuters / Markets"),
        ("site:reuters.com business",   "@Reuters / Business"),
        ("site:reuters.com technology", "@Reuters / Technology"),
        ("site:reuters.com world",      "@Reuters / World"),
    ]
    for q, name in gn_queries:
        try:
            items.extend(fetch_google_news(q, name, hl="en-US", gl="US", ceid="US:en"))
        except Exception:
            pass

    return dedupe(items)


def fetch_x_4accounts() -> List[Dict]:
    """
    @BloombergJapan / @business / @ReutersJapan / @Reuters
    の4アカウント相当のニュースをまとめて返す。
    """
    items: List[Dict] = []
    items.extend(fetch_bloomberg_japan_enhanced())
    items.extend(fetch_bloomberg_business_enhanced())
    items.extend(fetch_reuters_jp_direct())   # @ReutersJapan (既存流用)
    items.extend(fetch_reuters_direct_enhanced())  # @Reuters
    return dedupe(items)


# ============================================================
# ★ 追加: X (Twitter) 本物のツイート取得（TwitterAPI.io 経由）
#
#  対象アカウント:
#    - @DeItaone (Walter Bloomberg)  英語キーワードで絞り込み
#    - @FirstSquawk                  英語キーワードで絞り込み
#    - @financialjuice               英語キーワードで絞り込み
#    - @Yuto_Headline                「*」で始まるツイートだけ
#
#  特徴:
#    - 10分ごとのキャッシュ（過剰な API 呼び出しを防ぐ）
#    - アカウント間 5 秒待機（レート制限 429 を回避）
#    - 429 が出たら自動リトライ（最大2回、8秒・15秒の待機）
#    - 翻訳しない（英語のまま表示）
# ============================================================

# 英語アカウント用キーワード（このどれかを含むツイートだけ拾う）
X_REAL_KEYWORDS_EN = [
    "Japan", "Japanese", "yen", "BOJ", "Tokyo", "Nikkei",
    "Fed", "Trump", "tariff", "China", "rate", "dollar",
    "oil", "intervention", "inflation",
]

# キャッシュ（プロセス内メモリ。10分間保持）
# ★ @st.cache_resource で「スクリプト再実行をまたいで同じ辞書」を返すようにする。
#   これをしないと、Streamlit は画面操作（サイドバーを開く等）のたびに module-level
#   の代入を再実行して辞書を新規生成してしまうため、バックグラウンドスレッドが
#   書き込んだ diag をサイドバーから読めなくなる（＝「まだ取得を試みていません」が
#   ずっと出続ける現象）。
@st.cache_resource
def _get_x_real_cache() -> Dict[str, object]:
    return {
        "items": [],
        "fetched_at": 0,  # UNIX time
        "diag": {
            "last_attempt_at": 0,  # 最後にAPI呼び出しを試みた時刻
            "last_error": "",      # 直近のエラー（あれば）
            "per_account": [],     # [{"handle":..., "raw":N, "passed":N, "status":...}, ...]
            "secrets_ok": None,    # APIキーがSecretsから読めたか
        },
    }

_X_REAL_CACHE: Dict[str, object] = _get_x_real_cache()

# ★ 取得スケジュール: 毎時 :01, :16, :31, :46 に取得（指標発表 :00/:30 の1分後）
#   各スロットの中では1回だけ取得し、次のスロットが始まるまでキャッシュを返す。
_X_REAL_SLOT_MARKS = [1, 16, 31, 46]  # 分（0-59）


def _get_current_slot_start(now_epoch: int) -> int:
    """
    現在時刻が属するスロットの開始 epoch を返す。
    例: 12:23 → 12:16 のepoch / 12:31:05 → 12:31 のepoch / 12:00:30 → 11:46 のepoch
    """
    hour_start = now_epoch - (now_epoch % 3600)
    minute_in_hour = (now_epoch - hour_start) // 60

    # 現在の分以下で最大のスロット境界を探す
    current_slot_mark = None
    for m in _X_REAL_SLOT_MARKS:
        if m <= minute_in_hour:
            current_slot_mark = m
        else:
            break

    if current_slot_mark is None:
        # 0分台の時は前の時間の :46 スロットが最新
        return hour_start - 3600 + 46 * 60
    return hour_start + current_slot_mark * 60


def _get_next_slot_start(now_epoch: int) -> int:
    """
    次のスロットの開始 epoch を返す（診断パネルで「次回取得予定」を表示するため）。
    """
    hour_start = now_epoch - (now_epoch % 3600)
    minute_in_hour = (now_epoch - hour_start) // 60

    # 現在の分より大きい最小のスロット境界を探す
    for m in _X_REAL_SLOT_MARKS:
        if m > minute_in_hour:
            return hour_start + m * 60
    # 全部過ぎていたら次の時間の :01
    return hour_start + 3600 + _X_REAL_SLOT_MARKS[0] * 60


# ============================================================
# ★ 翻訳機能（Claude Haiku 4.5）
#
#  FirstSquawk の英語ツイートを日本語に翻訳する。
#  - 同じ英文は1回しか翻訳しない（翻訳キャッシュで節約）
#  - キーが無い・API失敗の場合は英文のまま返す（アプリは止めない）
#  - タイトル1行目だけ翻訳（コスト最小化）
# ============================================================
@st.cache_resource
def _get_translation_cache() -> Dict[str, object]:
    """
    英文 → 日本語訳 のキャッシュ。プロセス生存中は保持。
    diag は翻訳の診断情報（試行回数・成功数・失敗数・直近エラー）。
    """
    return {
        "map": {},  # {english_text: japanese_text}
        "diag": {
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "last_error": "",
            "secrets_ok": None,
        },
    }


def _translate_to_japanese(english_text: str) -> str:
    """
    英文を日本語に翻訳して返す。
    - 空文字や日本語が混じってる場合などは元の文を返す
    - 数値・記号だけのテキスト（例: "156.23", "+0.5%"）は翻訳しない（Claude APIコスト節約）
    - Claude API キーが無ければ元の文を返す
    - 翻訳に失敗したら元の文を返す（アプリは止めない）
    - キャッシュにあれば即返す
    """
    if not english_text or not english_text.strip():
        return english_text

    # ★ 数値・記号だけのテキストは翻訳しない
    #   アルファベット3文字以上が含まれていなければ「翻訳する意味なし」と判断
    #   例:
    #     "156.23"        → アルファベット0文字 → 翻訳しない
    #     "+0.5%"         → アルファベット0文字 → 翻訳しない
    #     "JPY: 156.45"   → アルファベット3文字 → 翻訳する
    #     "JAPAN Q1 GDP"  → アルファベット多数 → 翻訳する
    letter_count = sum(1 for c in english_text if c.isalpha())
    if letter_count < 3:
        return english_text

    tc = _get_translation_cache()
    tmap = tc["map"]
    tdiag = tc["diag"]

    # ★ v2: プロンプトのバージョンを含めることで、プロンプト変更時に古い翻訳キャッシュを
    # 自動的に無効化する。「トランプ前大統領」等の古い訳が再利用されるのを防ぐ。
    _TRANSLATE_PROMPT_VERSION = "v2-strict-no-title"
    cache_key = f"{_TRANSLATE_PROMPT_VERSION}::{english_text.strip()}"
    if cache_key in tmap:
        return tmap[cache_key]

    # Claude API キーを Secrets から取得
    try:
        api_key = st.secrets["claude"]["api_key"]
        tdiag["secrets_ok"] = True
    except Exception as e:
        tdiag["secrets_ok"] = False
        tdiag["last_error"] = "Secrets読み込み失敗: " + type(e).__name__
        return english_text  # 鍵が無いので元の英文を返す

    if not api_key:
        tdiag["secrets_ok"] = False
        tdiag["last_error"] = "APIキーが空"
        return english_text

    # Anthropic ライブラリで翻訳
    tdiag["attempts"] += 1
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
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
        # message.content は list of content blocks
        translated = ""
        if message.content and len(message.content) > 0:
            translated = (message.content[0].text or "").strip()

        if not translated:
            tdiag["failures"] += 1
            tdiag["last_error"] = "翻訳結果が空"
            return english_text

        tmap[cache_key] = translated
        tdiag["successes"] += 1
        return translated

    except Exception as e:
        tdiag["failures"] += 1
        tdiag["last_error"] = type(e).__name__ + ": " + str(e)[:120]
        return english_text  # 失敗時は元の英文を返す


def _twitterapi_io_search(query: str, api_key: str) -> Tuple[List[Dict], str]:
    """
    TwitterAPI.io の advanced_search を叩く。
    429 (Too Many Requests) が返ったら最大2回、待機を入れて再試行する。
    戻り値: (tweets, status_text)
      status_text は診断用。"ok"、"http:404"、"timeout"、"exception:..." など。
    """
    url = (
        "https://api.twitterapi.io/twitter/tweet/advanced_search"
        "?query=" + requests.utils.quote(query) + "&queryType=Latest"
    )
    headers = {"X-API-Key": api_key}

    max_attempts = 3
    wait_seconds = [0, 8, 15]

    last_status = "unknown"

    for attempt in range(max_attempts):
        if wait_seconds[attempt] > 0:
            time.sleep(wait_seconds[attempt])

        try:
            r = requests.get(url, headers=headers, timeout=20)
        except Exception as e:
            last_status = "exception:" + type(e).__name__
            return [], last_status

        if 200 <= r.status_code < 300:
            try:
                data = r.json()
            except Exception:
                return [], "json_parse_error"
            tweets = data.get("tweets")
            if isinstance(tweets, list):
                return tweets, "ok:" + str(len(tweets))
            return [], "no_tweets_key"

        last_status = "http:" + str(r.status_code)
        # 429 ならリトライ、それ以外のエラーは即終了
        if r.status_code == 429 and attempt < max_attempts - 1:
            continue
        return [], last_status

    return [], last_status


def fetch_x_real_tweets() -> List[Dict]:
    """
    TwitterAPI.io 経由で、4アカウントの本物のツイートを取得。
    結果は10分間キャッシュする。
    タイトルにはHTMLタグを入れない（描画側で is_breaking フラグを見て赤くする）。
    """
    import sys as _sys
    print("[fetch_x_real_tweets] called", file=_sys.stderr, flush=True)

    # ★ シングルトン辞書をローカル変数に取り出して、関数内で確実に同じ辞書を読み書きする。
    #   （module level の _X_REAL_CACHE は Streamlit の再実行で別オブジェクトに
    #     なる可能性があるため、ここで明示的に singleton getter を呼ぶ）
    cache = _get_x_real_cache()
    print(f"[fetch_x_real_tweets] cache id={id(cache)}", file=_sys.stderr, flush=True)

    # ★ スロット方式キャッシュ判定:
    #   毎時 :01, :16, :31, :46 を境に新しい「取得スロット」が始まる。
    #   このスロットの中で既に取得済み（fetched_at >= slot_start）ならキャッシュを返す。
    #   こうすることで、指標発表 :00/:30 の直後 (:01/:31) に確実に取得され、
    #   かつ各スロット内で重複取得は起きない。
    now = int(time.time())
    slot_start = _get_current_slot_start(now)
    if cache["fetched_at"] >= slot_start:
        print(f"[fetch_x_real_tweets] within current slot (fetched_at={cache['fetched_at']}, slot_start={slot_start}), returning {len(cache['items'])} cached items", file=_sys.stderr, flush=True)
        return list(cache["items"])

    # 診断情報を初期化
    diag = cache["diag"]
    diag["last_attempt_at"] = now
    diag["last_error"] = ""
    diag["per_account"] = []
    diag["secrets_ok"] = None
    print(f"[fetch_x_real_tweets] diag initialized, last_attempt_at={now}", file=_sys.stderr, flush=True)

    # APIキーを Streamlit Secrets から取得
    try:
        api_key = st.secrets["twitterapi"]["api_key"]
        diag["secrets_ok"] = True
    except Exception as e:
        # キーが未設定なら空で返す（アプリ全体は止めない）
        diag["secrets_ok"] = False
        diag["last_error"] = "Secrets読み込み失敗: " + type(e).__name__
        return []

    if not api_key or api_key == "ここに①TwitterAPI.ioのキー":
        diag["secrets_ok"] = False
        diag["last_error"] = "APIキーが空またはプレースホルダーのまま"
        return []

    # 直近 60 分のツイートを対象にする（30分だと取りこぼしが多いので拡張）
    since_ts = now - 60 * 60
    since_str = time.strftime("%Y-%m-%d_%H:%M:%S_UTC", time.gmtime(since_ts))

    # アカウントごとの設定
    # source は2アカウント両方「速報」に統一（Xアカウント名は出さない）
    # FirstSquawk: filter="none" → 全ツイート通過（英語のままだと読めないので、後で翻訳される）
    # Yuto_Headline: filter="asterisk" → 「*」または「＊」で始まるツイートのみ通過（日本語なので翻訳不要）
    account_configs = [
        {"handle": "FirstSquawk",    "filter": "none",     "source": "速報", "translate": True},
        {"handle": "Yuto_Headline",  "filter": "asterisk", "source": "速報", "translate": False},
    ]

    items: List[Dict] = []

    for i, acc in enumerate(account_configs):
        # 2件目以降は前のアカウントから5秒待つ（レート制限回避）
        if i > 0:
            time.sleep(5)

        query = "from:" + acc["handle"] + " since:" + since_str
        tweets, status = _twitterapi_io_search(query, api_key)

        raw_count = len(tweets) if tweets else 0
        passed_count = 0

        for t in tweets:
            text = t.get("text") or ""
            if not text:
                continue

            # リプライ・リツイート除外
            if t.get("isReply"):
                continue
            if text.startswith("RT @"):
                continue
            if t.get("retweeted_tweet"):
                continue

            # アカウント別の絞り込み
            if acc["filter"] == "asterisk":
                # 「*」または「＊」(全角)で始まるツイートだけ通す
                stripped = text.lstrip(" \t\r\n\"'＂　")
                if not (stripped.startswith("*") or stripped.startswith("＊")):
                    continue
            elif acc["filter"] == "keywords":
                # キーワードを含むツイートだけ通す
                hit = False
                text_lower = text.lower()
                for kw in X_REAL_KEYWORDS_EN:
                    if kw.lower() in text_lower:
                        hit = True
                        break
                if not hit:
                    continue
            elif acc["filter"] == "none":
                # 全部通過（フィルターなし）
                pass

            # ツイート本文の1行目をタイトルにする
            first_line = text.split("\n", 1)[0].strip()
            raw_title = first_line[:160] if first_line else text[:160]

            # ★ 翻訳が必要なアカウント（FirstSquawk等）は raw_title を日本語に翻訳
            if acc.get("translate"):
                display_title = _translate_to_japanese(raw_title)
            else:
                display_title = raw_title

            # ★ タイトルにHTMLタグは入れない（描画側で is_breaking を見て赤くする）
            #    プレーンテキストの「🔴速報 」プレフィックスだけ付けておくと、
            #    万一描画側のフラグ判定が外れても見た目で速報と分かる。
            title = "🔴速報 " + display_title

            # ツイートURL
            tweet_url = t.get("url") or ""
            if not tweet_url:
                tid = t.get("id")
                if tid:
                    tweet_url = "https://x.com/" + acc["handle"] + "/status/" + str(tid)

            if not tweet_url or not title:
                continue

            # 投稿時刻（あれば文字列のまま入れる。描画側は first_seen を使う）
            published = t.get("createdAt") or None

            items.append({
                "source": acc["source"],
                "title": title,
                "url": tweet_url,
                "published": published,
                "is_breaking": True,  # ← 描画側でこれを見て赤色スタイルを当てる
            })
            passed_count += 1

        # アカウント単位の診断情報を記録
        diag["per_account"].append({
            "handle": acc["handle"],
            "status": status,
            "raw": raw_count,
            "passed": passed_count,
        })

    # キャッシュ更新（シングルトン辞書に対して）
    cache["items"] = items
    cache["fetched_at"] = now

    return dedupe(items)


# ============================================================
# ★ 追加: X (Twitter) トレンド・カテゴリ別キーワード抽出
#
#  X 本体は API 認証が必要だが、Twitter のトレンドだけを集計する公開サイト
#  （trends24.in / getdaytrends.com）は Cookie 不要・スクレイピング可能。
#  ここから日本の現在のトレンド単語のみを取得し、米株/日本株/為替/政治/経済
#  に関係する単語のみを抽出してニュース項目化する。
#  各単語は X 検索 URL（live フィルタ）にリンクさせる。
# ============================================================

# 各カテゴリにマッチさせるキーワード（部分一致・大小文字区別なし）
TWITTER_TREND_CATEGORIES: Dict[str, List[str]] = {
    "米株": [
        "ナスダック", "nasdaq", "ダウ", "dow", "s&p", "sp500", "spx",
        "米株", "ny株", "ニューヨーク株", "us株", "wall street", "ウォール街",
        "apple", "アップル", "tesla", "テスラ", "nvidia", "エヌビディア",
        "microsoft", "マイクロソフト", "google", "グーグル", "alphabet",
        "meta", "メタ", "amazon", "アマゾン", "netflix", "ネットフリックス",
        "amd", "intel", "インテル", "broadcom", "ブロードコム", "mag7",
        "fomc", "frb", "fed", "パウエル", "powell", "米金利", "米長期金利",
        "米cpi", "雇用統計", "ism",
    ],
    "日本株": [
        "日経", "日経平均", "topix", "東証", "日本株", "日本株式",
        "プライム市場", "グロース市場", "スタンダード市場",
        "ストップ高", "ストップ安", "増配", "減配", "自社株買い",
        "決算", "上方修正", "下方修正", "業績予想", "ipo",
        "日経先物", "先物", "日経225",
        "ソフトバンク", "トヨタ", "ソニー", "任天堂", "ファストリ",
        "アドテスト", "アドバンテスト", "東エレク",
    ],
    "為替": [
        "ドル円", "円相場", "為替", "usdjpy", "usd/jpy",
        "ユーロ円", "eurjpy", "ポンド円", "gbpjpy",
        "豪ドル円", "audjpy", "ユーロドル", "eurusd",
        "円安", "円高", "介入", "為替介入", "覆面介入",
        "ドル高", "ドル安", "fx", "為替市場",
    ],
    "政治": [
        "国会", "首相", "総理", "総裁", "政府", "内閣",
        "自民党", "自民", "立憲民主", "立憲", "公明", "維新", "国民民主", "れいわ",
        "政治", "選挙", "解散", "解散総選挙", "衆院選", "参院選",
        "外相", "防衛省", "外務省", "財務省", "経産省", "総務省",
        "トランプ", "trump", "バイデン", "biden", "ハリス", "harris",
        "プーチン", "putin", "習近平", "ゼレンスキー", "zelensky",
        "ホワイトハウス", "議会", "上院", "下院",
    ],
    "経済": [
        "経済", "景気", "gdp", "物価", "賃金", "ベア", "春闘",
        "インフレ", "デフレ", "スタグフレ",
        "金利", "利上げ", "利下げ", "ゼロ金利", "マイナス金利",
        "日銀", "boj", "植田", "黒田",
        "cpi", "ppi", "原油", "wti", "ブレント", "天然ガス",
        "失業率", "金融政策", "量的緩和", "qe", "qt",
        "ecb", "ラガルド", "lagarde", "imf",
    ],
}


def fetch_twitter_trends_categorized() -> List[Dict]:
    """
    日本の Twitter トレンドキーワードを公開サイトから取得し、
    米株/日本株/為替/政治/経済 に該当する単語のみを抽出して返す。

    取得元（順番に試行 → 全て試して結果を統合）:
      ① https://trends24.in/japan/
      ② https://getdaytrends.com/japan/
    """
    from urllib.parse import quote_plus

    SOURCES_TO_TRY = [
        "https://trends24.in/japan/",
        "https://getdaytrends.com/japan/",
    ]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }

    trends_raw: List[str] = []

    for url in SOURCES_TO_TRY:
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")

            # trends24.in: <ol class="trend-card__list"><li><a>WORD</a></li>
            # getdaytrends: <table class="ranking">...<a>WORD</a>
            selectors = [
                "ol.trend-card__list li a",   # trends24.in
                "div.trend-card ol li a",     # trends24.in fallback
                "table.ranking a",            # getdaytrends.com
                "td.main a",                  # getdaytrends.com cells
                "a.trend-link",               # 一般的な命名
            ]
            local_count = 0
            for sel in selectors:
                for el in soup.select(sel):
                    text = el.get_text(strip=True)
                    text = re.sub(r"\s+", " ", text).strip()
                    # 不要要素フィルタ
                    if not text or len(text) < 2 or len(text) > 80:
                        continue
                    if re.fullmatch(r"[\d,\.]+", text):
                        continue
                    if text.startswith("http"):
                        continue
                    trends_raw.append(text)
                    local_count += 1
                if local_count > 0:
                    break  # この URL から取得できたので次のセレクタは試さない
        except Exception:
            continue

    # 重複除去（順序維持）
    seen: set = set()
    trends_unique: List[str] = []
    for t in trends_raw:
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        trends_unique.append(t)

    # カテゴリにマッチするものだけ抽出
    items: List[Dict] = []
    now_utc = datetime.now(timezone.utc)
    matched_keys: set = set()

    for trend in trends_unique:
        trend_lower = trend.lower()
        matched_categories: List[str] = []
        for category, keywords in TWITTER_TREND_CATEGORIES.items():
            for kw in keywords:
                if kw.lower() in trend_lower:
                    matched_categories.append(category)
                    break  # この category の中はもう見ない

        if not matched_categories:
            continue

        cat_label = " / ".join(matched_categories)
        # X 検索 URL（live＝最新タブ）
        search_url = f"https://x.com/search?q={quote_plus(trend)}&f=live"

        dedup_key = f"{cat_label}::{trend}"
        if dedup_key in matched_keys:
            continue
        matched_keys.add(dedup_key)

        items.append({
            "source":    f"X トレンド／{cat_label}",
            "title":     trend,
            "url":       search_url,
            "published": now_utc,
        })

    return items


# ============================================================
# ★ 追加: SBI証券 ファンドレポート一覧
#
#  https://www.sbisec.co.jp/.../fund_report.html
#  ファンドレポート（レポート名＋URL）をスクレイピング。
#  ページは shift_jis のため encoding を明示的に判定する。
# ============================================================
def fetch_sbi_fund_reports() -> List[Dict]:
    """
    SBI証券のファンドレポート一覧をスクレイピング。
    """
    from urllib.parse import urljoin

    SOURCE = "SBI証券／ファンドレポート"
    url = (
        "https://www.sbisec.co.jp/ETGate/?OutSide=on"
        "&_ControlID=WPLETmgR001Control"
        "&_DataStoreID=DSWPLETmgR001Control"
        "&burl=search_fund&dir=info%2F&file=fund_report.html"
        "&cat1=fund&cat2=report&getFlg=on"
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Referer": "https://www.sbisec.co.jp/",
    }

    items: List[Dict] = []
    seen_urls: set = set()

    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        # SBI証券は shift_jis ベース → 自動判定
        if not r.encoding or r.encoding.lower() in ("iso-8859-1",):
            r.encoding = r.apparent_encoding or "shift_jis"

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")

        date_re = re.compile(r"(20\d{2})[/年.\-](\d{1,2})[/月.\-](\d{1,2})")

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            text = a.get_text(separator=" ", strip=True)
            text = re.sub(r"\s+", " ", text).strip()

            if not text or len(text) < 6:
                continue

            href_lower = href.lower()
            # ファンドレポート関連リンクのみ採用
            is_report_link = (
                ".pdf" in href_lower
                or "fund_research" in href_lower
                or "fund_report" in href_lower
                or "search_fund" in href_lower
                or "report" in href_lower
            )
            if not is_report_link:
                continue

            # 絶対URL化
            full_url = urljoin(url, href)
            full_url_key = full_url.split("#")[0]
            if full_url_key in seen_urls:
                continue
            # ナビゲーション/不要リンクを除外
            if any(skip in full_url_key.lower() for skip in [
                "javascript:", "mailto:", "/help/", "/login",
            ]):
                continue
            seen_urls.add(full_url_key)

            if not is_probably_title(text):
                continue
            if len(text) < 8:
                continue

            # 周辺要素から日付を抽出
            published: Optional[datetime] = None
            parent = a.parent
            for _ in range(4):
                if not parent:
                    break
                parent_text = parent.get_text(separator=" ", strip=True)
                m = date_re.search(parent_text)
                if m:
                    try:
                        published = datetime(
                            int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            tzinfo=timezone.utc,
                        )
                        break
                    except Exception:
                        pass
                parent = parent.parent

            items.append({
                "source":    SOURCE,
                "title":     text,
                "url":       full_url_key,
                "published": published,
            })
    except Exception:
        pass

    return dedupe(items)


# -----------------------------
# Background Collector
# -----------------------------
class BackgroundCollector:
    def __init__(self):
        self.lock = threading.Lock()
        self.data: Dict[str, object] = {}
        self.last_updated: Optional[str] = None
        self.interval_sec: int = 30  # ★デフォルト30秒
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # 初回検知時刻（first_seen）を保持
        self.seen_at: Dict[str, float] = {}

    def set_interval(self, sec: int):
        sec = int(sec)
        if sec < 3:
            sec = 3
        with self.lock:
            self.interval_sec = sec

    def attach_first_seen(self, items: List[Dict]) -> List[Dict]:
        now_utc = datetime.now(timezone.utc)
        out = []
        with self.lock:
            for it in items:
                k = item_key(it)
                if not k:
                    out.append(it)
                    continue
                if k not in self.seen_at:
                    self.seen_at[k] = now_utc.timestamp()
                ts = self.seen_at.get(k)
                try:
                    it["first_seen"] = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                except Exception:
                    it["first_seen"] = None
                out.append(it)
        return out

    def start(self, fetch_fn):
        if self._thread and self._thread.is_alive():
            return

        def loop():
            while not self._stop.is_set():
                try:
                    new_data = fetch_fn()
                    with self.lock:
                        self.data = new_data
                        self.last_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass

                with self.lock:
                    sec = int(self.interval_sec)
                for _ in range(sec):
                    if self._stop.is_set():
                        break
                    time.sleep(1)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def snapshot(self):
        with self.lock:
            return dict(self.data), self.last_updated, int(self.interval_sec)


@st.cache_resource
def get_collector() -> BackgroundCollector:
    return BackgroundCollector()

# -----------------------------
# UI render
# -----------------------------
def render_items(items: List[Dict], limit: int, show_source: bool, show_time: bool, title_px: int, compact: bool,
                 auto_scroll: bool = False, scroll_speed: int = 60, scroll_height: int = 600):
    if compact:
        st.markdown(
            """
            <style>
            .news-row { margin: 1px 0 !important; }
            .news-title { line-height: 1.10 !important; }
            hr { margin: 6px 0 !important; }
            </style>
            """,
            unsafe_allow_html=True,
        )

    st.markdown(
        f"""
        <style>
        .news-title {{ font-size: {title_px}px; }}
        </style>
        """,
        unsafe_allow_html=True,
    )

    if not items:
        st.info("取得できる見出しがありませんでした。")
        return

    # --- 全行を HTML 文字列に組み立て ---
    # ★ 速報用の赤色スタイル（is_breaking=True の行にだけ適用）
    from html import escape as _html_escape_local
    rows_html = """
    <style>
    .news-title.is-breaking { color: #d32f2f; font-weight: bold; }
    </style>
    """
    shown = 0
    for it in items:
        if shown >= limit:
            break

        title = (it.get("title") or "").strip()
        url = (it.get("url") or "").strip()
        src = (it.get("source") or "").strip()
        is_breaking = bool(it.get("is_breaking"))

        dt = it.get("published")
        if not isinstance(dt, datetime):
            dt = it.get("first_seen")

        meta_bits = []
        if show_time and isinstance(dt, datetime):
            meta_bits.append(fmt_dt(dt))
        if show_source and src:
            meta_bits.append(src)
        meta = " / ".join(meta_bits)

        # ★ タイトルはエスケープ（HTMLタグが含まれていてもプレーンに表示）
        title_html = _html_escape_local(title)
        title_class = "news-title is-breaking" if is_breaking else "news-title"

        rows_html += f"""
        <div class="news-row" style="border-bottom:1px solid rgba(128,128,128,0.15); padding:4px 0;">
          <div class="news-open"><a href="{url}" target="_blank" rel="noopener noreferrer">Open</a></div>
          <div class="{title_class}">
            {title_html}
            {"<span class='news-meta'>(" + _html_escape_local(meta) + ")</span>" if meta else ""}
          </div>
        </div>
        """
        shown += 1

    if auto_scroll:
        # ── JS新着プッシュ方式 ──────────────────────────────────────────
        # ・新着（localStorage未登録）→ 上に黄色ハイライトで追加
        # ・既読（前回以前に表示済み）→ 下に
        # ・全件をlocalStorageに記録して次回更新時に引き継ぐ

        import json as _json

        items_data = []
        shown = 0
        for it in items:
            if shown >= limit:
                break
            title = (it.get("title") or "").strip()
            url   = (it.get("url")   or "").strip()
            src   = (it.get("source") or "").strip()
            is_breaking = bool(it.get("is_breaking"))
            dt    = it.get("published")
            if not isinstance(dt, datetime):
                dt = it.get("first_seen")
            dt_str = fmt_dt(dt) if isinstance(dt, datetime) else ""
            meta_parts = []
            if show_time and dt_str:
                meta_parts.append(dt_str)
            if show_source and src:
                meta_parts.append(src)
            meta = " / ".join(meta_parts)
            key  = (url or title).strip()
            items_data.append({
                "key": key, "title": title, "url": url, "meta": meta,
                "is_breaking": is_breaking,
            })
            shown += 1

        items_json = _json.dumps(items_data, ensure_ascii=False)

        push_html = f"""
        <script>
        // Streamlit のテーマを親フレームから検出
        (function() {{
            try {{
                const bg = window.getComputedStyle(window.parent.document.body).backgroundColor;
                const rgb = bg.match(/\d+/g);
                if (rgb) {{
                    const luminance = (parseInt(rgb[0])*299 + parseInt(rgb[1])*587 + parseInt(rgb[2])*114) / 1000;
                    document.documentElement.setAttribute('data-theme', luminance < 128 ? 'dark' : 'light');
                }}
            }} catch(e) {{
                // クロスオリジンの場合はダークモードを優先
                if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {{
                    document.documentElement.setAttribute('data-theme', 'dark');
                }} else {{
                    document.documentElement.setAttribute('data-theme', 'light');
                }}
            }}
        }})();
        </script>
        <style>
        :root[data-theme="dark"] {{
            --bg:        #0e1117;
            --fg:        #fafafa;
            --border:    rgba(255,255,255,0.12);
            --border2:   rgba(255,255,255,0.07);
            --btn-bg:    rgba(255,255,255,0.08);
            --btn-fg:    #c8d0e0;
            --btn-bdr:   rgba(255,255,255,0.2);
            --meta-fg:   rgba(255,255,255,0.5);
            --new-hl:    rgba(255,210,0,0.22);
        }}
        :root[data-theme="light"] {{
            --bg:        #ffffff;
            --fg:        #1a1a2e;
            --border:    rgba(0,0,0,0.12);
            --border2:   rgba(0,0,0,0.07);
            --btn-bg:    rgba(0,0,0,0.04);
            --btn-fg:    #31333f;
            --btn-bdr:   rgba(49,51,63,0.25);
            --meta-fg:   rgba(0,0,0,0.5);
            --new-hl:    rgba(255,210,0,0.35);
        }}
        html, body {{
            margin: 0; padding: 0;
            background: var(--bg);
            color: var(--fg);
            font-family: "Source Sans Pro", sans-serif;
        }}
        #news-feed {{
            height: {scroll_height}px;
            overflow-y: auto;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 6px 10px;
            box-sizing: border-box;
            background: var(--bg);
        }}
        .nrow {{
            display: flex;
            align-items: baseline;
            gap: 10px;
            border-bottom: 1px solid var(--border2);
            padding: 3px 0;
        }}
        .ntitle {{
            font-size: {title_px}px;
            line-height: 1.15;
            color: var(--fg);
        }}
        /* ★ 速報用：赤太字 */
        .ntitle.is-breaking {{
            color: #d32f2f;
            font-weight: bold;
        }}
        .nrow.is-new {{
            animation: flowDown 5s ease-out forwards;
            transform-origin: top;
        }}
        @keyframes flowDown {{
            0%   {{
                transform: translateY(-28px) scaleY(0.6);
                opacity: 0;
                background: var(--new-hl);
            }}
            18%  {{
                transform: translateY(0) scaleY(1);
                opacity: 1;
                background: var(--new-hl);
            }}
            100% {{
                transform: translateY(0) scaleY(1);
                opacity: 1;
                background: transparent;
            }}
        }}
        .nbtn a {{
            display: inline-block;
            padding: 1px 8px;
            border: 1px solid var(--btn-bdr);
            border-radius: 8px;
            text-decoration: none;
            font-size: 12px;
            white-space: nowrap;
            color: var(--btn-fg);
            background: var(--btn-bg);
        }}
        .nbtn a:hover {{ opacity: 0.75; }}
        .nmeta {{
            color: var(--meta-fg);
            font-size: 12px;
            white-space: nowrap;
        }}
        #news-status {{
            font-size: 11px;
            color: var(--meta-fg);
            margin-top: 4px;
        }}
        </style>
        <div id="news-feed"></div>
        <div id="news-status"></div>
        <script>
        (function() {{
            const ITEMS    = {items_json};
            const SEEN_KEY = 'news_seen_v2';
            const feed     = document.getElementById('news-feed');
            const status   = document.getElementById('news-status');

            // localStorage から既読セットを復元（iframe内なのでwindow.parent経由）
            let seenArr = [];
            try {{
                const raw = window.parent.localStorage.getItem(SEEN_KEY);
                seenArr = raw ? JSON.parse(raw) : [];
            }} catch(e) {{}}
            const seenSet = new Set(seenArr);

            // 新着 / 既読に分類
            const newItems = ITEMS.filter(it => it.key && !seenSet.has(it.key));
            const oldItems = ITEMS.filter(it => !it.key || seenSet.has(it.key));

            // 全件を既読登録（最大3000件に制限）
            ITEMS.forEach(it => {{ if (it.key) seenSet.add(it.key); }});
            try {{
                window.parent.localStorage.setItem(SEEN_KEY, JSON.stringify([...seenSet].slice(-3000)));
            }} catch(e) {{}}

            function esc(s) {{
                return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
            }}

            function makeRow(it, isNew) {{
                const row = document.createElement('div');
                row.className = 'nrow' + (isNew ? ' is-new' : '');
                // ★ is_breaking のときは ntitle に is-breaking クラスを足す → 赤太字
                const titleClass = it.is_breaking ? 'ntitle is-breaking' : 'ntitle';
                row.innerHTML =
                    '<div class="nbtn"><a href="' + esc(it.url) + '" target="_blank" rel="noopener noreferrer">Open</a></div>' +
                    '<div class="' + titleClass + '">' + esc(it.title) +
                    (it.meta ? '<span class="nmeta"> (' + esc(it.meta) + ')</span>' : '') +
                    '</div>';
                return row;
            }}

            // 新着を上、既読を下に描画
            [...newItems, ...oldItems].forEach(function(it, i) {{
                feed.appendChild(makeRow(it, i < newItems.length));
            }});

            feed.scrollTop = 0;

            const newLabel = newItems.length > 0 ? '🆕 新着 ' + newItems.length + '件　' : '';
            status.textContent = newLabel + '計 ' + ITEMS.length + '件　▼スクロールで続きを見る';
        }})();
        </script>
        """
        # components.html でレンダリング（st.markdownはscriptを実行しないため）
        components.html(push_html, height=scroll_height + 40, scrolling=False)
    else:
        st.markdown(rows_html, unsafe_allow_html=True)

# -----------------------------
# Fetch all sources (for background thread)
# -----------------------------
def fetch_all_sources(collector: BackgroundCollector) -> Dict[str, object]:
    # Bloomberg 英語（Google News / 複数カテゴリ）
    bloomberg_en = fetch_bloomberg_en_all()

    # Bloomberg 日本語: bloomberg.co.jp（日本ドメイン）+ bloomberg.com の日本語記事
    _bbja_1 = fetch_google_news("site:bloomberg.co.jp", "Bloomberg JP (bloomberg.co.jp)", hl="ja", gl="JP", ceid="JP:ja")
    _bbja_2 = fetch_google_news("site:bloomberg.com/japanese", "Bloomberg JP (bloomberg.com/jp)", hl="ja", gl="JP", ceid="JP:ja")
    _bbja_3 = fetch_google_news("bloomberg.com/jp", "Bloomberg JP (bloomberg.com/jp)", hl="ja", gl="JP", ceid="JP:ja")
    bloomberg_ja = dedupe(_bbja_1 + _bbja_2 + _bbja_3)

    # Reuters 日本語: Google News + jp.reuters.com 直接取得
    _rtja_google = fetch_google_news("site:reuters.com OR site:jp.reuters.com", "Reuters JP (Google News)", hl="ja", gl="JP", ceid="JP:ja")
    _rtja_direct = fetch_reuters_jp_direct()
    reuters_ja = dedupe(_rtja_google + _rtja_direct)
    reuters_en = fetch_reuters_en()

    nikkei = fetch_google_news("site:nikkei.com", "日経(Google News)", hl="ja", gl="JP", ceid="JP:ja")
    nikkei = filter_nikkei_exclude_jinji(nikkei)

    nikkei_cookie = fetch_nikkei_cookie()
    nikkei_cookie = filter_nikkei_exclude_jinji(nikkei_cookie)

    nsj_url = "https://www.nsjournal.jp/category/nsj_short_live/sokuhou/"
    nsj = fetch_nsj_sokuhou(nsj_url, "日本証券新聞(速報・市況)")
    # ☆フィルターを廃止 → 全記事表示

    wsj_en = fetch_google_news("site:wsj.com", "WSJ(Google News)", hl="en-US", gl="US", ceid="US:en")
    wsj_ja = fetch_google_news("site:jp.wsj.com", "WSJ日本語(Google News)", hl="ja", gl="JP", ceid="JP:ja")

    nikkei225jp_res = fetch_nikkei225jp_news_all1()
    nikkei225jp_items = nikkei225jp_res["items"]
    nikkei225jp_debug = nikkei225jp_res["debug"]

    # TBS NEWS DIG（Bloomberg提携記事一覧）
    tbs_bloomberg = fetch_tbs_newsdig_bloomberg()

    # ★ 読売新聞（政治・経済・海外） / 産経新聞（経済・政治）
    yomiuri = fetch_yomiuri()
    sankei  = fetch_sankei()

    # first_seen を付与
    bloomberg_en      = collector.attach_first_seen(bloomberg_en)
    bloomberg_ja      = collector.attach_first_seen(bloomberg_ja)
    reuters_ja        = collector.attach_first_seen(reuters_ja)
    reuters_en        = collector.attach_first_seen(reuters_en)
    nikkei            = collector.attach_first_seen(nikkei)
    nikkei_cookie     = collector.attach_first_seen(nikkei_cookie)
    nsj               = collector.attach_first_seen(nsj)
    wsj_en            = collector.attach_first_seen(wsj_en)
    wsj_ja            = collector.attach_first_seen(wsj_ja)
    nikkei225jp_items = collector.attach_first_seen(nikkei225jp_items)
    tbs_bloomberg     = collector.attach_first_seen(tbs_bloomberg)
    yomiuri           = collector.attach_first_seen(yomiuri)
    sankei            = collector.attach_first_seen(sankei)

    # ── X 関連の取得（互いに巻き込まれないよう、それぞれ個別に try/except で防護） ──
    import sys as _sys

    # X ホームタイムライン
    try:
        x_home = fetch_x_home_timeline(max_results=100)
    except Exception as _e:
        print(f"[x_home] EXCEPTION: {type(_e).__name__}: {_e}", file=_sys.stderr, flush=True)
        x_home = []
    x_home = collector.attach_first_seen(x_home)

    # X 4アカウント（@BloombergJapan / @business / @ReutersJapan / @Reuters）
    try:
        x_4accounts = fetch_x_4accounts()
    except Exception as _e:
        print(f"[x_4accounts] EXCEPTION: {type(_e).__name__}: {_e}", file=_sys.stderr, flush=True)
        x_4accounts = []
    x_4accounts = collector.attach_first_seen(x_4accounts)

    # ★ X 本物ツイート（@DeItaone / @FirstSquawk / @financialjuice / @Yuto_Headline）
    #    TwitterAPI.io 経由で取得、10分キャッシュ
    print("[x_real] about to call fetch_x_real_tweets()", file=_sys.stderr, flush=True)
    try:
        x_real = fetch_x_real_tweets()
        print(f"[x_real] returned {len(x_real)} items", file=_sys.stderr, flush=True)
    except Exception as _e:
        print(f"[x_real] EXCEPTION: {type(_e).__name__}: {_e}", file=_sys.stderr, flush=True)
        x_real = []
    x_real = collector.attach_first_seen(x_real)

    # ★ X トレンド（カテゴリ別キーワード抽出: 米株/日本株/為替/政治/経済）
    try:
        x_trends = fetch_twitter_trends_categorized()
    except Exception as _e:
        print(f"[x_trends] EXCEPTION: {type(_e).__name__}: {_e}", file=_sys.stderr, flush=True)
        x_trends = []
    x_trends = collector.attach_first_seen(x_trends)

    # ★ SBI証券 ファンドレポート
    sbi_fund = fetch_sbi_fund_reports()
    sbi_fund = collector.attach_first_seen(sbi_fund)

    # All（7本まとめ）bloomberg_en を bloomberg の代表として使用
    all_7 = []
    for lst in [bloomberg_en, bloomberg_ja, reuters_ja, nikkei, nsj, wsj_en, wsj_ja]:
        all_7.extend(lst)
    all_7 = dedupe(all_7)
    all_7 = sort_items_by_effective_time_desc(all_7)

    # All（8本まとめ）= All(7) + nikkei225jp
    all_8 = list(all_7) + list(nikkei225jp_items)
    all_8 = dedupe(all_8)
    all_8 = sort_items_by_effective_time_desc(all_8)

    # All（全部まとめ）= 全ソース統合（★ X トレンド・SBI証券・読売・産経も含む）
    all_full = []
    for lst in [bloomberg_en, bloomberg_ja,
                reuters_en, reuters_ja,
                nikkei, nikkei_cookie,
                nsj, wsj_en, wsj_ja, nikkei225jp_items,
                x_home, x_4accounts, x_real, x_trends,
                tbs_bloomberg, sbi_fund,
                yomiuri, sankei]:
        all_full.extend(lst)
    all_full = dedupe(all_full)
    all_full = sort_items_by_effective_time_desc(all_full)

    # ★ Yahoo! 由来の記事をすべてのソースから二重に除外（ユーザー指定）
    # source か URL に "yahoo" "ヤフー" を含む場合スキップ。
    def _drop_yahoo(items: List[Dict]) -> List[Dict]:
        out = []
        for it in items or []:
            try:
                src = (it.get("source") or "").lower()
                url = (it.get("url") or "").lower()
                if "yahoo" in src or "ヤフー" in (it.get("source") or "") or "yahoo.co.jp" in url or "yahoo.com" in url:
                    continue
            except Exception:
                pass
            out.append(it)
        return out

    all_full          = _drop_yahoo(all_full)
    nikkei225jp_items = _drop_yahoo(nikkei225jp_items)
    bloomberg_en      = _drop_yahoo(bloomberg_en)
    bloomberg_ja      = _drop_yahoo(bloomberg_ja)
    reuters_en        = _drop_yahoo(reuters_en)
    reuters_ja        = _drop_yahoo(reuters_ja)
    nikkei            = _drop_yahoo(nikkei)
    nikkei_cookie     = _drop_yahoo(nikkei_cookie)
    nsj               = _drop_yahoo(nsj)
    wsj_en            = _drop_yahoo(wsj_en)
    wsj_ja            = _drop_yahoo(wsj_ja)
    tbs_bloomberg     = _drop_yahoo(tbs_bloomberg)
    sbi_fund          = _drop_yahoo(sbi_fund)
    yomiuri           = _drop_yahoo(yomiuri)
    sankei            = _drop_yahoo(sankei)

    return {
        "all_full":        all_full,
        "all_8":           all_8,
        "all_7":           all_7,
        "nikkei225jp_items": sort_items(dedupe(nikkei225jp_items)),
        "nikkei225jp_debug": nikkei225jp_debug,
        "bloomberg_en":    sort_items(bloomberg_en),
        "bloomberg_ja":    sort_items(bloomberg_ja),
        "reuters_en":      sort_items(reuters_en),
        "reuters_ja":      sort_items(reuters_ja),
        "nikkei":          sort_items(dedupe(nikkei)),
        "nikkei_cookie":   sort_items(dedupe(nikkei_cookie)),
        "nsj":             sort_items(dedupe(nsj)),
        "wsj_en":          sort_items(wsj_en),
        "wsj_ja":          sort_items(wsj_ja),
        "nsj_url":         nsj_url,
        "x_home":          sort_items(x_home),
        "x_4accounts":     sort_items_by_effective_time_desc(dedupe(x_4accounts)),
        "x_real":          sort_items_by_effective_time_desc(dedupe(x_real)),
        "x_trends":        sort_items_by_effective_time_desc(dedupe(x_trends)),
        "tbs_bloomberg":   sort_items_by_effective_time_desc(dedupe(tbs_bloomberg)),
        "sbi_fund":        sort_items_by_effective_time_desc(dedupe(sbi_fund)),
        "yomiuri":         sort_items_by_effective_time_desc(dedupe(yomiuri)),
        "sankei":          sort_items_by_effective_time_desc(dedupe(sankei)),
    }

# -----------------------------
# Sidebar settings
# -----------------------------
st.title("News Headline Monitor")
st.caption("見出し＋リンクのみを取得します（本文は取得しません）。")

st.sidebar.header("設定")

# ★デフォルト30秒
refresh_sec = st.sidebar.number_input(
    "更新間隔（秒）",
    min_value=3,
    max_value=3600,
    value=int(st.session_state.get("refresh_sec", 30)),
    step=1,
    key="refresh_sec",
)

auto_on = st.sidebar.toggle(
    f"自動更新（{int(refresh_sec)}秒）",
    value=True,
    key="auto_on",
)

# ★デフォルト160本・文字サイズ16px (ユーザー指定)
limit = st.sidebar.slider("表示件数", 10, 200, int(st.session_state.get("limit_default", 160)), 5)
title_px = st.sidebar.slider("見出し（ヘッドライン本文）文字サイズ(px)", 12, 26, 16, 1)
compact = st.sidebar.toggle("詰めて表示（余白少なめ）", value=True)
show_source = st.sidebar.toggle("出典を表示", value=True)
show_time = st.sidebar.toggle("日時を表示", value=True)

st.sidebar.markdown("---")
st.sidebar.subheader("📜 新着プッシュ表示")
auto_scroll = st.sidebar.toggle("新着を上に自動追加", value=True, key="auto_scroll")
scroll_height = st.sidebar.slider(
    "表示エリア高さ（px）",
    min_value=200, max_value=1200,
    value=int(st.session_state.get("scroll_height_val", 700)),
    step=50,
)
scroll_speed = 120  # 旧ticker用・互換のため残す（未使用）

# -----------------------------
# Start background collection (server side)
# -----------------------------
collector = get_collector()
collector.set_interval(int(refresh_sec))
collector.start(lambda: fetch_all_sources(collector))

data_snapshot, last_updated, running_interval = collector.snapshot()

# -----------------------------
# UI Auto Refresh
# -----------------------------
# ✅ 旧: 30秒ごとに window.parent.location.reload() で画面全体リロード（→ チャートも毎回フリッカー）
# ✅ 新: ニュース欄だけ st.fragment(run_every=refresh_sec) で再描画。
#        チャート(components.html) はフラグメント外に居るので一切再読込されない。
#        ブラウザがタブを非表示にすると JS タイマー(=fragment polling)も自動で間引かれ、
#        タブに戻った瞬間にすぐ次の rerun が走るので、可視復帰時の更新も自然に効く。

# st.fragment は Streamlit 1.37+ (1.38 で正式名に昇格)
_fragment_decorator = getattr(st, "fragment", None) or getattr(st, "experimental_fragment", None)
_frag_run_every = int(refresh_sec) if (auto_on and _fragment_decorator is not None) else None

if _fragment_decorator is not None:
    @_fragment_decorator(run_every=_frag_run_every)
    def _render_news_fragment(data_key: str):
        """
        ニュース欄だけを定期再描画するフラグメント。
        毎回 collector.snapshot() を読み直すのでバックグラウンド収集の最新が反映される。
        """
        fresh_snapshot, _fresh_last, _ = collector.snapshot()
        if not isinstance(fresh_snapshot, dict):
            fresh_snapshot = {}
        items = fresh_snapshot.get(data_key, []) or []
        render_items(
            items, int(limit), show_source, show_time, title_px, compact,
            auto_scroll, scroll_speed, scroll_height,
        )
else:
    # Streamlit が古くて st.fragment が無い場合のフォールバック（自動更新なし）
    st.warning(
        "Streamlit 1.37+ にアップグレードすると、ページ全体をリロードせず "
        "ニュース欄だけをストリーム更新できます。`pip install -U streamlit`"
    )
    def _render_news_fragment(data_key: str):
        items = data_snapshot.get(data_key, []) or []
        render_items(
            items, int(limit), show_source, show_time, title_px, compact,
            auto_scroll, scroll_speed, scroll_height,
        )

st.sidebar.caption(f"収集更新（サーバー側）：{running_interval} 秒ごと")
st.sidebar.caption(f"最終収集時刻：{last_updated if last_updated else '（収集中…）'}")

# -----------------------------
# ★ 速報X取得の診断パネル（折りたたみ式）
# -----------------------------
with st.sidebar.expander("🔴 速報X取得の状況（診断）", expanded=False):
    # ★ ここでもシングルトン経由で取得（module level の _X_REAL_CACHE は
    #   再実行で別オブジェクトになりうるので、毎回 _get_x_real_cache() を呼んで
    #   バックグラウンドスレッドと同じ辞書を確実に読む）
    _cache_now = _get_x_real_cache()
    st.caption(f"🔬 cache dict id (デバッグ): {id(_cache_now)}")  # 書き手と一致してるか確認用
    _diag = _cache_now.get("diag", {}) or {}
    _ok = _diag.get("secrets_ok")
    if _ok is True:
        st.success("✅ APIキー：Secretsから読込済み")
    elif _ok is False:
        st.error("❌ APIキー：読込失敗 / 空")
    else:
        st.info("⏳ APIキー：まだ取得を試みていません")

    if _diag.get("last_error"):
        st.error("直近エラー: " + str(_diag["last_error"]))

    _last_attempt = _diag.get("last_attempt_at", 0)
    if _last_attempt:
        _ago = max(0, int(time.time()) - int(_last_attempt))
        st.caption(f"最終API試行: {_ago}秒前")
    else:
        st.caption("最終API試行: まだなし")

    # ★ 次回取得予定の時刻を表示（毎時 :01, :16, :31, :46）
    _now_epoch = int(time.time())
    _next_slot = _get_next_slot_start(_now_epoch)
    _wait_seconds = max(0, _next_slot - _now_epoch)
    _wait_min = _wait_seconds // 60
    _wait_sec = _wait_seconds % 60
    # JST (UTC+9) で表示
    _next_jst = datetime.fromtimestamp(_next_slot, tz=timezone.utc).astimezone(timezone(timedelta(hours=9)))
    st.caption(f"次回取得予定: {_next_jst.strftime('%H:%M')} JST（あと{_wait_min}分{_wait_sec}秒）")

    _cached_n = len(_cache_now.get("items", []) or [])
    st.caption(f"キャッシュ内のツイート件数: {_cached_n}件")

    _per = _diag.get("per_account", []) or []
    if _per:
        st.caption("アカウント別（直近の取得結果）:")
        for row in _per:
            st.caption(
                f"  • @{row.get('handle')}: "
                f"生{row.get('raw',0)}件 → 通過{row.get('passed',0)}件 "
                f"[{row.get('status','?')}]"
            )
    else:
        st.caption("アカウント別データなし（まだ未取得）")

    # ── 翻訳（Claude Haiku 4.5）の診断情報 ──
    st.markdown("---")
    st.caption("**翻訳（Claude Haiku 4.5）**")
    _tc = _get_translation_cache()
    _tdiag = _tc.get("diag", {}) or {}
    _tok = _tdiag.get("secrets_ok")
    if _tok is True:
        st.success("✅ Claude APIキー：Secretsから読込済み")
    elif _tok is False:
        st.error("❌ Claude APIキー：未設定 / 読込失敗（英文のまま表示されます）")
    else:
        st.info("⏳ Claude APIキー：まだ翻訳呼び出しが行われていません")

    if _tdiag.get("last_error"):
        st.error("翻訳の直近エラー: " + str(_tdiag["last_error"]))

    st.caption(
        f"翻訳: 試行{_tdiag.get('attempts',0)}回 / "
        f"成功{_tdiag.get('successes',0)}回 / "
        f"失敗{_tdiag.get('failures',0)}回"
    )
    _tmap_n = len(_tc.get("map", {}) or {})
    st.caption(f"翻訳キャッシュ: {_tmap_n}件保持中（同じ英文は2回目以降タダ）")

if not data_snapshot:
    st.info("初回の取得中です。数秒待ってから自動的に更新されます。")
    st.stop()

# -----------------------------
# Tabs (radio) — 削除済み: 常に All（全部まとめ）固定表示
# -----------------------------
st.markdown("<hr />", unsafe_allow_html=True)

# -----------------------------
# Realtime Charts (above news): 日経225 CFD / NASDAQ100先物 / ドル円
# -----------------------------
# TradingView の symbol-overview ウィジェットを 3 つ並べてリアルタイム表示。
# 価格は TradingView 側の WebSocket で自動更新されます。
#
# ★ ウィジェットタイプの選択について ★
#   "overview" : embed-widget-symbol-overview.js
#                → ヘッダーに **価格・前日比・前日比% を大きな文字で常に固定表示**。
#                  画面幅が狭くなっても price が省略・縮小されにくい。
#                  CFD 銘柄でも % が確実に表示される（前日終値メタを内部計算）。
#   "mini"     : embed-widget-mini-symbol-overview.js
#                → コンパクト（同じ高さでチャート部分が広い）だが、
#                  ① 狭い画面で price ヘッダーが省略されることがある
#                  ② 前日終値メタを持たない CFD で % が空欄になりやすい
#   → 当アプリではプライス可読性を優先し全銘柄 "overview" を採用。

# 万一いずれかのシンボルが空表示になる場合は、下記の代替に差し替えてください:
#   日経225 CFD:     "OANDA:JP225USD"   → "FOREXCOM:JP225" / "CAPITALCOM:J225" /
#                                          "VANTAGE:NIKKEI225" (JPY建て表記) /
#                                          "TVC:NI225" (現物指数・日中のみ更新)
#                                          注: OANDA:JP225USD は決済通貨が USD なだけで、
#                                          価格気配は日経指数ポイント（約60,000台）。
#   NASDAQ100先物:    "FOREXCOM:NAS100"  → "FX:NAS100" / "CAPITALCOM:US100" /
#                                          "OANDA:NAS100USD" / "NASDAQ:NDX" (US時間のみ)
#   ドル円:           "FX:USDJPY"        → "OANDA:USDJPY" / "FX_IDC:USDJPY"
#
# 各エントリ: (表示ラベル, TradingViewシンボル, ウィジェット種別, Yahoo Tickerで前営業日比計算)
# Yahoo Ticker: ^N225/^NDX (現物=休場日は動かない) ではなく CME先物 (≒24h動く) を採用
#   NIY=F = CME Nikkei 225 Yen Futures (円建て、ほぼ24h)
#   NQ=F  = CME E-mini NASDAQ-100 Futures (USD建て、ほぼ24h)
#   JPY=X = USD/JPY スポット (24h)
_CHART_SPECS = [
    # (label, tv_symbol, kind, yahoo_ticker_chain, cash_ref_ticker)
    # cash_ref_ticker: 指定されると、prev_closeをこのティッカー(現物指数)の直近完了セッション終値で上書き
    #                  → バッジの % が「現物終値からの変化率」になる(NASDAQ等の米国祝日対策)
    ("日経平均(24h CFD)",     "OANDA:JP225USD",     "overview", "^N225,NKD=F,NIY=F",          None),    # 日経はOSE先物がほぼ現物連動
    ("NASDAQ100(24h CFD)",    "OANDA:NAS100USD",    "overview", "NQ=F,MNQ=F,^NDX,^IXIC",      "^NDX"),  # 現在価格は先物、prev_closeは^NDX現物
    ("ドル円",                "FX:USDJPY",          "overview", "USDJPY=X,JPY=X",             None),    # FXは24h、現物概念なし
]


_PCT_CACHE: Dict[str, Tuple[float, Optional[str], Optional[str], Optional[str]]] = {}
_PCT_CACHE_TTL_SEC = 120   # 2分 (短くして古いキャッシュを残さない)
_PCT_STALE_TTL_SEC = 3600  # 1時間: フェッチ全失敗時に古いキャッシュを使う上限
_PCT_CACHE_VERSION = "v8"   # v8: cash_ref_ticker でprev_closeを現物終値に上書き対応

# ★ NEW (v6): 各データソースの試行結果を記録するための診断辞書。
# キー: "{yahoo_ticker}:{source}" (例: "NQ=F:tv", "NQ=F:cme")
# 値: 文字列 ("OK", "HTTP403", "empty", "EXC:Timeout" 等)
# UI下部の debug 折りたたみセクションに表示してデバッグに使う。
_SOURCE_DIAG: Dict[str, str] = {}
# ★ NEW (v6): 各銘柄で最終的に採用されたソース名 (例: "tv", "cme", "yahoo-v8")
_FINAL_SOURCE: Dict[str, str] = {}

# Stooq へのティッカーマッピング
_STOOQ_MAP = {
    "^N225":    "^nkx",       # 日経平均
    "NKD=F":    "nkd.f",
    "NIY=F":    "niy.f",
    "NQ=F":     "nq.f",       # CME E-mini NASDAQ-100
    "MNQ=F":    "nq.f",       # ※stooqにMicroは無いのでNQで代用 (同じ値動き)
    "^NDX":     "^ndx",       # NASDAQ-100 現物
    "^IXIC":    "^ixic",      # NASDAQ Composite
    "USDJPY=X": "usdjpy",
    "JPY=X":    "usdjpy",
}


# ★ NEW (v4): CME Group 公開API のプロダクトIDマッピング
# CMEは自社の delayed quote 用に公開のJSON APIを提供している:
#   https://www.cmegroup.com/CmeWS/mvc/Quotes/Future/{productId}/{venue}
# venue "G" = Globex (electronic, ほぼ24時間取引)
# レスポンスの quotes[0] = front-month (最も活発な限月)。
# 各quote: { code, last, change, percentageChange, priorSettle, volume, ... }
# Yahoo の NQ=F が祝日にstale化する問題を回避するため、futures系は CME を最優先で叩く。
_CME_PRODUCT_IDS = {
    "NQ=F":  ("8074", "G"),   # E-mini NASDAQ-100
    "MNQ=F": ("8920", "G"),   # Micro E-mini NASDAQ-100
    "NKD=F": ("249",  "G"),   # Nikkei 225 USD (CME)
    "NIY=F": ("250",  "G"),   # Nikkei 225 Yen (CME)
    "ES=F":  ("133",  "G"),   # E-mini S&P 500
}


# ★ NEW (v7): TradingView Scanner API 用シンボルマッピング (複数候補)
# 診断ログから判明: /global/scan は到達可能だが、シンボル形式によって empty が返る。
# 各Yahooティッカーに対して [(symbol, market), ...] のリストを定義し、順番に試す。
# market = 'futures' / 'cfd' / 'forex' / 'global' / 'america' のいずれか。
_TV_SYMBOL_MAP = {
    # NASDAQ系: futures が一番確実、次に CFD、最後に現物
    "NQ=F": [
        ("CME_MINI:NQ1!",        "futures"),   # 連続限月先物 (front-month continuous)
        ("CME_MINI:NQM2026",     "futures"),   # 2026年6月限 明示
        ("CAPITALCOM:US100",     "cfd"),       # Capital.com の US100 CFD
        ("OANDA:NAS100USD",      "cfd"),       # OANDA CFD
        ("FOREXCOM:NSXUSD",      "cfd"),       # FOREX.com
        ("PEPPERSTONE:NAS100",   "cfd"),
        ("TVC:NDX",              "global"),    # TV指数版
    ],
    "MNQ=F": [
        ("CME_MINI:MNQ1!",       "futures"),
        ("CME_MINI:NQ1!",        "futures"),
        ("CAPITALCOM:US100",     "cfd"),
        ("OANDA:NAS100USD",      "cfd"),
    ],
    "^NDX": [
        ("CME_MINI:NQ1!",        "futures"),   # 現物指数の代わりに先物を使う(休場日対策)
        ("NASDAQ:NDX",           "america"),
        ("TVC:NDX",              "global"),
        ("OANDA:NAS100USD",      "cfd"),
    ],
    "^IXIC": [
        ("CME_MINI:NQ1!",        "futures"),
        ("NASDAQ:IXIC",          "america"),
        ("TVC:IXIC",             "global"),
    ],
    # 日経系: 同じ思想で先物→CFD→現物
    "^N225": [
        ("OSE:NK2251!",          "futures"),   # 大阪取引所 日経225先物 連続限月
        ("CME:NKD1!",            "futures"),   # CME 日経USD先物
        ("OANDA:JP225USD",       "cfd"),
        ("CAPITALCOM:J225",      "cfd"),
        ("TVC:NI225",            "global"),
        ("INDEX:NKY",            "global"),
    ],
    "NKD=F": [
        ("CME:NKD1!",            "futures"),
        ("OANDA:JP225USD",       "cfd"),
        ("CAPITALCOM:J225",      "cfd"),
    ],
    "NIY=F": [
        ("CME:NIY1!",            "futures"),
        ("OSE:NK2251!",          "futures"),
        ("OANDA:JP225JPY",       "cfd"),
    ],
    # ドル円: global で OK が確認できているのでそのまま
    "USDJPY=X": [
        ("FX:USDJPY",            "global"),
        ("OANDA:USDJPY",         "forex"),
        ("FX_IDC:USDJPY",        "global"),
    ],
    "JPY=X": [
        ("FX:USDJPY",            "global"),
        ("OANDA:USDJPY",         "forex"),
    ],
}


def _http_get_with_ua(url: str, timeout: int = 6):
    """User-Agent付きで HTTP GET。失敗時は None"""
    try:
        r = requests.get(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
        }, timeout=timeout)
        if r.status_code == 200:
            return r
    except Exception:
        pass
    return None


def _fetch_tradingview_quote(yahoo_ticker: str):
    """★ NEW (v5): TradingView Scanner API から (last, prev_close) を取得。

    埋め込みチャートと同じシンボル (OANDA:NAS100USD 等) を直接叩くので、
    バッジに表示する値 = チャートに表示される値 が保証される最強の整合性。

    エンドポイント: POST https://scanner.tradingview.com/symbol
    本家TVのフロントエンドが使っている内部APIで、認証不要・24h稼働。
    レスポンス: {"data":[{"s":"OANDA:NAS100USD","d":[29930.75, 1.26, 372.0]}]}
                                                       ↑close ↑%   ↑abs change

    返り値: (last, prev_close) or (None, None)
    """
    candidates = _TV_SYMBOL_MAP.get(yahoo_ticker)
    if not candidates:
        return None, None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
    }
    attempts = []  # 各試行の結果ログ
    for tv_symbol, market in candidates:
        url = f"https://scanner.tradingview.com/{market}/scan"
        body = {
            "symbols": {"tickers": [tv_symbol], "query": {"types": []}},
            "columns": ["close", "change", "change_abs"],
        }
        try:
            r = requests.post(url, json=body, timeout=5, headers=headers)
            if r.status_code != 200:
                attempts.append(f"{tv_symbol}@{market}=HTTP{r.status_code}")
                continue
            data = r.json()
            rows = data.get("data") or []
            if not rows:
                attempts.append(f"{tv_symbol}@{market}=empty")
                continue
            d = rows[0].get("d") or []
            if len(d) < 3 or d[0] is None or d[2] is None:
                attempts.append(f"{tv_symbol}@{market}=bad-shape")
                continue
            last = float(d[0])
            change_abs = float(d[2])
            prev = last - change_abs
            if last > 0 and prev > 0:
                attempts.append(f"{tv_symbol}@{market}=OK")
                _SOURCE_DIAG[yahoo_ticker + ":tv"] = (
                    f"OK[{tv_symbol}@{market}] tried={len(attempts)}"
                )
                return last, prev
            attempts.append(f"{tv_symbol}@{market}=zero-or-neg")
        except Exception as e:
            attempts.append(f"{tv_symbol}@{market}=EXC:{type(e).__name__}")
            continue
    # 全敗 → 試行内容を診断ログに残す (最後の3個を表示)
    _SOURCE_DIAG[yahoo_ticker + ":tv"] = "NG: " + " | ".join(attempts[-3:])
    return None, None


def _fetch_cme_quote(yahoo_ticker: str):
    """★ NEW (v4): CME Group 公開API から先物の (last, prior_settle) を取得。

    CME本家サイト (https://www.cmegroup.com/markets/equities.html) が表示している
    値そのもの (Globex 取引中の最新値 + 前営業日settlement) を返す。
    本家サイトと完全に整合するため、futures系はこれが最優先ソース。

    祝日 (Memorial Day等) でも Globex は稼働しているため、Yahooがstale化しても
    CMEは正常に値を返す。

    返り値: (last, prior_settle) or (None, None)
    """
    info = _CME_PRODUCT_IDS.get(yahoo_ticker)
    if not info:
        return None, None
    product_id, venue = info
    try:
        url = f"https://www.cmegroup.com/CmeWS/mvc/Quotes/Future/{product_id}/{venue}"
        # CMEはAccept-LanguageやRefererを見ることがあるので強化したヘッダーを使う
        try:
            r = requests.get(url, headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.cmegroup.com/markets/equities.html",
            }, timeout=6)
        except Exception as e:
            _SOURCE_DIAG[yahoo_ticker + ":cme"] = f"EXC:{type(e).__name__}"
            return None, None
        if r.status_code != 200:
            _SOURCE_DIAG[yahoo_ticker + ":cme"] = f"HTTP{r.status_code}"
            return None, None
        try:
            data = r.json()
        except Exception:
            _SOURCE_DIAG[yahoo_ticker + ":cme"] = "non-json"
            return None, None
        quotes = data.get("quotes") or []
        if not quotes:
            _SOURCE_DIAG[yahoo_ticker + ":cme"] = "empty"
            return None, None

        def _parse_num(v):
            """CMEの数値文字列をパース ("29,930.75" → 29930.75)"""
            if v is None:
                return None
            if isinstance(v, (int, float)):
                return float(v)
            if not isinstance(v, str):
                return None
            s = v.replace(",", "").replace("+", "").strip()
            if s in ("", "-", "—", "NA"):
                return None
            try:
                return float(s)
            except ValueError:
                return None

        # quotes[0] = front-month (CMEの並びは出来高優先で、通常は最も活発な限月が先頭)
        # ただし "last" が "-" のケースもあるので、有効な値を持つ最初の限月を採用
        for q in quotes:
            last = (
                _parse_num(q.get("last"))
                or _parse_num(q.get("lastTradedPrice"))
                or _parse_num(q.get("close"))
            )
            prev = (
                _parse_num(q.get("priorSettle"))
                or _parse_num(q.get("previousSettle"))
            )
            # 'last' が取れない場合は priorSettle + change から逆算を試みる
            if last is None and prev is not None:
                ch = _parse_num(q.get("change"))
                if ch is not None:
                    last = prev + ch
            if last is not None and prev is not None and last > 0 and prev > 0:
                _SOURCE_DIAG[yahoo_ticker + ":cme"] = "OK"
                return last, prev
        _SOURCE_DIAG[yahoo_ticker + ":cme"] = "no-valid-quote"
        return None, None
    except Exception as e:
        _SOURCE_DIAG[yahoo_ticker + ":cme"] = f"EXC:{type(e).__name__}"
        return None, None


def _fetch_yahoo_direct(ticker: str):
    """Yahoo Finance 公開API を直接叩いて (last_price, prev_close) を返す。

    取得順:
      ① meta.chartPreviousClose で前営業日終値を取得 (これは安定)
      ② intraday(5分足)の最新closeを「現在価格」候補として収集
      ③ meta.regularMarketPrice も候補に追加
      ④ 候補の中で「前営業日終値と異なる」値を優先採用 (=取引中の動き)
    """
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=5m&range=1d&includePrePost=true"
        r = _http_get_with_ua(url)
        if r is None:
            return None, None
        data = r.json()
        result = data.get("chart", {}).get("result")
        if not result:
            return None, None
        meta = result[0].get("meta", {})

        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        if prev_close is None:
            return None, None
        prev_close = float(prev_close)

        # 現在価格の候補を複数集める
        current_candidates = []

        # 候補A: intraday(5m)データの最新非null close (最もリアルタイム性高い)
        quote = result[0].get("indicators", {}).get("quote", [{}])[0]
        closes = quote.get("close") or []
        for c in reversed(closes):
            if c is not None:
                current_candidates.append(float(c))
                break

        # 候補B: meta.regularMarketPrice (stale な場合もある)
        mp = meta.get("regularMarketPrice")
        if mp is not None:
            current_candidates.append(float(mp))

        if not current_candidates:
            return None, None

        # 前営業日終値と「異なる」値を優先 (=取引中の最新値)
        for cand in current_candidates:
            if cand > 0 and abs(cand - prev_close) > 0.001:
                return cand, prev_close

        # 全部 prev_close と同じ = まだ取引始まってない or データ更新遅延
        # この場合は None を返してフォールバックさせる
        return None, None
    except Exception:
        return None, None


def _fetch_yahoo_cash_close(yahoo_ticker: str):
    """★ NEW (v8): Yahoo Finance から「直近の完了した現物セッション終値」を取得。

    用途: 先物の現在価格 vs 現物の直近終値 で % を計算したい時に使う(NASDAQバッジ等)。

    動作:
      - 現物市場が今日休場(米国祝日等) → meta.regularMarketPrice = 直近営業日の終値
      - 現物市場が今日取引中           → meta.chartPreviousClose = 前営業日の終値
      - 現物市場が今日取引終了後        → meta.regularMarketPrice = 今日の終値
    marketState で分岐して、確実に「直近の完了したセッションの終値」を返す。

    返り値: float or None
    """
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_ticker}?interval=5m&range=1d&includePrePost=false"
        r = requests.get(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }, timeout=5)
        if r.status_code != 200:
            _SOURCE_DIAG[yahoo_ticker + ":cash"] = f"HTTP{r.status_code}"
            return None
        data = r.json()
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            _SOURCE_DIAG[yahoo_ticker + ":cash"] = "no-result"
            return None
        meta = result[0].get("meta") or {}
        market_state = (meta.get("marketState") or "").upper()
        rmp = meta.get("regularMarketPrice")
        cpc = meta.get("chartPreviousClose") or meta.get("previousClose")

        # 取引中 (REGULAR) なら、参照したいのは「昨日の終値」 = chartPreviousClose
        # 取引外 (CLOSED/PRE/POST) なら、参照したいのは「直近の終値」 = regularMarketPrice
        if market_state == "REGULAR":
            if cpc and cpc > 0:
                _SOURCE_DIAG[yahoo_ticker + ":cash"] = f"OK[chartPrevClose,{market_state}]"
                return float(cpc)
        else:
            if rmp and rmp > 0:
                _SOURCE_DIAG[yahoo_ticker + ":cash"] = f"OK[regMktPrice,{market_state}]"
                return float(rmp)
        # フォールバック
        if cpc and cpc > 0:
            _SOURCE_DIAG[yahoo_ticker + ":cash"] = f"OK[fallback-cpc,{market_state}]"
            return float(cpc)
        if rmp and rmp > 0:
            _SOURCE_DIAG[yahoo_ticker + ":cash"] = f"OK[fallback-rmp,{market_state}]"
            return float(rmp)
        _SOURCE_DIAG[yahoo_ticker + ":cash"] = f"no-value,{market_state}"
        return None
    except Exception as e:
        _SOURCE_DIAG[yahoo_ticker + ":cash"] = f"EXC:{type(e).__name__}"
        return None


def _fetch_stooq(ticker: str):
    """stooq.com から CSV を取って (last_close, prev_close) を返す"""
    try:
        stooq_sym = _STOOQ_MAP.get(ticker, ticker.lower())
        url = f"https://stooq.com/q/d/l/?s={stooq_sym}&i=d"
        r = _http_get_with_ua(url)
        if r is None:
            return None, None
        text = (r.text or "").strip()
        lines = text.split("\n")
        if len(lines) < 3:    # ヘッダー + 2行 必要
            return None, None
        # 最後の2行から終値を取る
        # 形式: Date,Open,High,Low,Close,Volume
        def _parse(line):
            cols = line.split(",")
            if len(cols) < 5:
                return None
            try:
                return float(cols[4])   # Close
            except ValueError:
                return None
        last = _parse(lines[-1])
        prev = _parse(lines[-2])
        if last is None or prev is None:
            return None, None
        return last, prev
    except Exception:
        return None, None


def _calc_prev_close_pct(yahoo_ticker: str, cash_ref_ticker: Optional[str] = None):
    """前営業日終値、現在値、変化率を取得。
    複数データソースを試行してプライス表示を最優先する。

    yahoo_ticker: 現在価格を取りに行くティッカー (futures含む)
    cash_ref_ticker: 指定時は prev_close をこのティッカー(現物指数)の直近完了セッション
                     終値で上書きする。% 表示が「現物終値からの変化率」になる。
                     NASDAQバッジ等の米国祝日対策。

    取得経路 (v8・各ティッカーごとに順番にトライ):
      ⓪ TradingView Scanner   (埋め込みチャートと完全同期・最優先)
      ① CME Group 公開API      (futures only・本家サイトと完全整合)
      ② Yahoo Direct API       (HTTP直接・ライブラリ非依存)
      ③ yfinance.fast_info     (ライブラリ経由・リアルタイム値)
      ④ stooq.com              (HTTP直接・別ソース)
      ⑤ yfinance.history       (日足・最後の手段)
      ⑥ 古いキャッシュ           (StaleTTL内なら採用)
    cash_ref_ticker 指定時は、(last, prev) 取得後に prev だけ現物終値で差し替え。

    返り値: (price_str, pct_str, color) or (None, None, None)
    """
    now_ts = time.time()
    # キャッシュキーに cash_ref も含めて区別
    cache_key = f"{_PCT_CACHE_VERSION}:{yahoo_ticker}:cash={cash_ref_ticker}"
    cached = _PCT_CACHE.get(cache_key)
    if cached is not None:
        ts, price_str, pct_str, color = cached
        # フレッシュキャッシュ
        if (now_ts - ts) < _PCT_CACHE_TTL_SEC and price_str is not None:
            return price_str, pct_str, color

    def _format_price(v: float) -> str:
        if v >= 1000:
            return f"{v:,.2f}"
        elif v >= 10:
            return f"{v:.3f}"
        else:
            return f"{v:.4f}"

    def _format_result(close_today: float, close_prev: float):
        pct = (close_today - close_prev) / close_prev * 100.0
        sign = "+" if pct >= 0 else ""
        color = "#16a34a" if pct >= 0 else "#dc2626"
        return _format_price(close_today), f"{sign}{pct:.2f}%", color

    # ★ NEW (v8): cash_ref_ticker が指定されていれば、現物指数の直近終値を先に取得しておく。
    # 各データソースで取れた prev_close は futures の prior settle なので、
    # ここで取った cash_prev_close で上書きすることで、% が「現物終値からの変化率」になる。
    cash_prev_close = None
    if cash_ref_ticker:
        cash_prev_close = _fetch_yahoo_cash_close(cash_ref_ticker)

    def _finalize(last_close: float, prev_close: float, source_tag: str):
        """共通の終端処理: cash_ref があれば prev を差し替え、フォーマット、キャッシュ、診断記録"""
        # cash_prev_close が取れていればそれで上書き
        effective_prev = cash_prev_close if (cash_prev_close and cash_prev_close > 0) else prev_close
        price_str, pct_str, color = _format_result(last_close, effective_prev)
        _PCT_CACHE[cache_key] = (now_ts, price_str, pct_str, color)
        ref_tag = f"+cash({cash_ref_ticker})" if (cash_prev_close and cash_prev_close > 0) else ""
        _FINAL_SOURCE[yahoo_ticker] = f"{source_tag}{ref_tag}"
        return price_str, pct_str, color

    tickers = [t.strip() for t in yahoo_ticker.split(",") if t.strip()]

    for tk in tickers:
        # ★★★ ⓪ TradingView Scanner API (埋め込みチャートと完全同期・最優先)
        last_close, prev_close = _fetch_tradingview_quote(tk)
        if last_close and prev_close and prev_close > 0:
            return _finalize(last_close, prev_close, f"tv:{tk}")

        # ★★ ① CME Group 公開API (futures only, 本家サイトと完全整合)
        last_close, prev_close = _fetch_cme_quote(tk)
        if last_close and prev_close and prev_close > 0:
            return _finalize(last_close, prev_close, f"cme:{tk}")

        # ★ ② Yahoo Direct API (v8 chart の meta.regularMarketPrice = リアルタイム値)
        last_close, prev_close = _fetch_yahoo_direct(tk)
        if last_close and prev_close and prev_close > 0:
            return _finalize(last_close, prev_close, f"yahoo-v8:{tk}")

        # ② yfinance.fast_info (ライブラリ経由・リアルタイム値)
        try:
            import yfinance as yf
            ticker = yf.Ticker(tk)
            fi = ticker.fast_info
            last = float(fi.last_price) if fi.last_price is not None else None
            prev = float(fi.previous_close) if fi.previous_close is not None else None
            if last and prev and prev > 0 and last > 0:
                return _finalize(last, prev, f"yfinance-fast:{tk}")
        except Exception:
            pass

        # ③ stooq.com (HTTP直接・別データソース)
        last_close, prev_close = _fetch_stooq(tk)
        if last_close and prev_close and prev_close > 0:
            return _finalize(last_close, prev_close, f"stooq:{tk}")

        # ④ yfinance.history (日足のみ・取引中はラスト確定終値しか取れず誤計算するので最後の手段)
        try:
            import yfinance as yf
            ticker = yf.Ticker(tk)
            hist = ticker.history(period="7d", interval="1d", auto_adjust=False)
            if hist is not None and len(hist) >= 2:
                close_today = float(hist["Close"].iloc[-1])
                close_prev = float(hist["Close"].iloc[-2])
                # 同じ値だと0%になってしまうので、安全チェック
                if close_prev > 0 and close_today > 0 and close_today != close_prev:
                    return _finalize(close_today, close_prev, f"yfinance-hist:{tk}")
        except Exception:
            pass

    # ⑤ 全部失敗 → 古いキャッシュ採用 (1時間以内なら表示)
    if cached is not None:
        ts, price_str, pct_str, color = cached
        if price_str is not None and (now_ts - ts) < _PCT_STALE_TTL_SEC:
            _FINAL_SOURCE[yahoo_ticker] = "stale-cache"
            return price_str, pct_str, color

    _FINAL_SOURCE[yahoo_ticker] = "FAILED-ALL"
    _PCT_CACHE[cache_key] = (now_ts, None, None, None)
    return None, None, None


def _tv_widget_block(symbol: str, kind: str = "mini", color_theme: str = "light") -> str:
    """
    kind = "mini"     : embed-widget-mini-symbol-overview.js（コンパクト・チャート＋%）
    kind = "overview" : embed-widget-symbol-overview.js     （% 表示が確実だが少し大きめ）
    color_theme       : "light" or "dark"
    """
    if kind == "overview":
        cfg = {
            "symbols": [[symbol + "|12M"]],  # 12ヶ月表示（祝日でも過去1年の値動きが見える）
            "chartOnly": True,
            "width": "100%",
            "height": "220",
            "locale": "ja",
            "colorTheme": color_theme,
            "isTransparent": True,
            "autosize": False,
            "showVolume": False,
            "showMA": False,
            "hideDateRanges": False,         # 期間切替ボタンを表示（1D/1M/3M/12M)
            "hideMarketStatus": True,
            "hideSymbolLogo": True,
            "scalePosition": "right",
            "scaleMode": "Normal",
        }
        src = "https://s3.tradingview.com/external-embedding/embed-widget-symbol-overview.js"
    else:
        cfg = {
            "symbol": symbol,
            "width": "100%",
            "height": "220",
            "locale": "ja",
            "dateRange": "1D",
            "colorTheme": color_theme,
            "isTransparent": True,
            "autosize": False,
            "chartOnly": True,
            "trendLineColor": "rgba(41, 98, 255, 1)",
            "underLineColor": "rgba(41, 98, 255, 0.3)",
            "underLineBottomColor": "rgba(41, 98, 255, 0)",
        }
        src = "https://s3.tradingview.com/external-embedding/embed-widget-mini-symbol-overview.js"

    return (
        '<div class="tradingview-widget-container">'
        '<div class="tradingview-widget-container__widget"></div>'
        f'<script type="text/javascript" src="{src}" async>'
        + json.dumps(cfg, ensure_ascii=False)
        + "</script>"
        "</div>"
    )


# ★ チャート3つを横並び表示。テーマ自動切替＋ラベル＋前営業日比%バッジ。
def _build_chart_payload(label, sym, kind, yahoo_ticker, cash_ref_ticker=None):
    """各チャートのpayloadを構築。現在価格と前営業日比%を含める"""
    price_str, pct_str, pct_color = _calc_prev_close_pct(yahoo_ticker, cash_ref_ticker=cash_ref_ticker)
    if pct_str is None:
        price_str = "—"
        pct_str = "—"
        pct_color = "#9ca3af"
    return {
        "label": label,
        "price": price_str,        # 例: "29,481.64" or "—"
        "pct": pct_str,            # 例: "+3.03%" or "—"
        "pct_color": pct_color,    # "#16a34a" / "#dc2626" / "#9ca3af"
        "light_html": _tv_widget_block(sym, kind, color_theme="light"),
        "dark_html":  _tv_widget_block(sym, kind, color_theme="dark"),
    }

_chart_payloads_json = json.dumps(
    [
        _build_chart_payload(label, sym, kind, yahoo, cash_ref)
        for label, sym, kind, yahoo, cash_ref in _CHART_SPECS
    ],
    ensure_ascii=False,
)
_chart_payloads_json = (
    _chart_payloads_json
    .replace("<", "\\u003c")
    .replace(">", "\\u003e")
    .replace("&", "\\u0026")
)

_charts_html = """
<style>
  #tv-charts-wrap {
      display: flex;
      gap: 6px;
      width: 100%;
      flex-wrap: nowrap;
      align-items: flex-start;
      margin-bottom: 6px;
  }
  #tv-charts-wrap > .tv-col {
      flex: 1 1 0;
      min-width: 0;
      overflow: hidden;
  }
  #tv-charts-wrap .tv-label {
      font-size: clamp(12px, 1.6vw, 15px);
      font-weight: 800;
      color: #111;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
        "Hiragino Kaku Gothic ProN", "Yu Gothic UI", "Meiryo", sans-serif;
      letter-spacing: 0.2px;
      margin: 0 0 2px 2px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
  }
  #tv-charts-wrap .tv-pct-row {
      margin: 0 0 4px 2px;
      font-size: 11px;
      line-height: 1.2;
      white-space: nowrap;
  }
  #tv-charts-wrap .tv-price {
      display: inline-block;
      font-size: 14px;
      font-weight: 800;
      color: #111;
      margin-right: 5px;
      font-family: "Helvetica Neue", "Yu Gothic UI", Arial, sans-serif;
  }
  #tv-charts-wrap .tv-pct-badge {
      display: inline-block;
      padding: 1px 6px;
      border-radius: 4px;
      color: #fff;
      font-weight: 700;
      font-size: 11px;
  }
  #tv-charts-wrap .tv-pct-caption {
      color: #777;
      font-size: 10px;
      margin-left: 4px;
  }
</style>
<div id="tv-charts-wrap"></div>
<script>
(function() {
    const PAYLOADS = __PAYLOADS_JSON__;
    const wrap = document.getElementById('tv-charts-wrap');

    function detectTheme() {
        try {
            const bg = window.getComputedStyle(window.parent.document.body).backgroundColor;
            const m = bg.match(/\\d+/g);
            if (m && m.length >= 3) {
                const r = parseInt(m[0]), g = parseInt(m[1]), b = parseInt(m[2]);
                const luminance = (r*299 + g*587 + b*114) / 1000;
                return luminance < 128 ? 'dark' : 'light';
            }
        } catch (e) {}
        if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
            return 'dark';
        }
        return 'light';
    }

    const theme = detectTheme();
    document.documentElement.setAttribute('data-tv-theme', theme);

    PAYLOADS.forEach(function(p) {
        const col = document.createElement('div');
        col.className = 'tv-col';

        // 1行目: タイトル (例: "日経平均(24h CFD)")
        const label = document.createElement('div');
        label.className = 'tv-label';
        label.textContent = p.label;
        col.appendChild(label);

        // 2行目: 現在価格 + 前営業日比%バッジ
        const pctRow = document.createElement('div');
        pctRow.className = 'tv-pct-row';
        // 現在価格
        const priceSpan = document.createElement('span');
        priceSpan.className = 'tv-price';
        priceSpan.textContent = p.price || '—';
        pctRow.appendChild(priceSpan);
        // % バッジ
        const pctBadge = document.createElement('span');
        pctBadge.className = 'tv-pct-badge';
        pctBadge.textContent = p.pct || '—';
        pctBadge.style.background = p.pct_color || '#9ca3af';
        pctBadge.title = '前営業日比';
        pctRow.appendChild(pctBadge);
        col.appendChild(pctRow);

        const holder = document.createElement('div');
        holder.innerHTML = theme === 'dark' ? p.dark_html : p.light_html;
        holder.querySelectorAll('script').forEach(function(oldScript) {
            const newScript = document.createElement('script');
            for (const attr of oldScript.attributes) {
                newScript.setAttribute(attr.name, attr.value);
            }
            newScript.text = oldScript.text;
            oldScript.parentNode.replaceChild(newScript, oldScript);
        });
        col.appendChild(holder);

        wrap.appendChild(col);
    });
})();
</script>
""".replace("__PAYLOADS_JSON__", _chart_payloads_json)

# ★追加: 合成FX (土日のみ) + 再計算カード (常時) を既存チャートの上に表示
# import失敗時は小さく赤バナーで通知（過去にファイル破損による無音失敗があったため）
if _SYNTHETIC_FX_IMPORT_ERROR:
    st.markdown(
        f'<div style="background:#ffebee;border:1px solid #d32f2f;padding:6px;'
        f'margin:4px 0;font-size:11px;color:#b71c1c;border-radius:4px;">'
        f'⚠️ 合成FX機能が読み込めません: {_SYNTHETIC_FX_IMPORT_ERROR}'
        f'</div>',
        unsafe_allow_html=True
    )
try:
    _fx_data = fetch_synthetic_fx()
    if _fx_data:
        render_synthetic_fx(_fx_data)
except Exception:
    pass  # 既存チャート・ニュース取得には一切影響させない

components.html(_charts_html, height=270)

# ★ NEW (v6→v8): データソース診断
# URLに ?debug=1 が付いている時だけ表示する。
# 通常閲覧時はスッキリした見た目、トラブル時は ?debug=1 で原因切り分け可能。
try:
    # query_params: ?debug=1 / ?debug=true / ?debug=on でON
    _debug_param = ""
    try:
        _debug_param = str(st.query_params.get("debug", "")).lower()
    except Exception:
        # 古いStreamlit用フォールバック
        try:
            _qp = st.experimental_get_query_params()
            _debug_param = str(_qp.get("debug", [""])[0]).lower()
        except Exception:
            _debug_param = ""

    if _debug_param in ("1", "true", "on", "yes"):
        with st.expander("🔧 データソース診断 (デバッグ用)", expanded=True):
            st.caption(
                "下段3チャートのバッジ値が、どのデータソースから取れたかを表示します。"
                "URLから `?debug=1` を外せばこのパネルは消えます。"
            )
            # 採用されたソース
            if _FINAL_SOURCE:
                st.markdown("**最終採用ソース**")
                st.code(
                    "\n".join(f"{k:32s} → {v}" for k, v in _FINAL_SOURCE.items()),
                    language=None,
                )
            # 各ソースの試行結果
            if _SOURCE_DIAG:
                st.markdown("**各データソースの試行結果**")
                st.code(
                    "\n".join(f"{k:32s} → {v}" for k, v in sorted(_SOURCE_DIAG.items())),
                    language=None,
                )
            if not _FINAL_SOURCE and not _SOURCE_DIAG:
                st.caption("(まだフェッチされていません。下段チャートが描画されるまで待ってください)")
except Exception:
    pass  # 診断UIで例外が出ても本体に影響させない

# -----------------------------
# Render: 常に「All（全部まとめ）」のニュース一覧のみ表示
# -----------------------------
_render_news_fragment("all_full")