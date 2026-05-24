"""
refresh_sekai_url.py (v3)

GitHub Actions から定期的に呼ばれて、sekai-kabuka.com の以下を取得する:

1) データファイルURL (.1.js)
   → sekai_data_url.txt に書き出し
   → synthetic_fx.py が q() データのフォールバック用に使用

2) ★NEW: ページに表示されている各銘柄の現在値テキスト (DOM スクレイプ)
   → sekai_indices.json に書き出し
   → synthetic_fx.py が "本物のサンデーCFD現在値" として最優先で使用

Exit code:
  0: 1 もしくは 2 の少なくとも一方が成功
  1: 両方失敗
"""

import asyncio
import os
import re
import sys
import time
import json
import gzip
from datetime import datetime, timezone

import urllib.request
from playwright.async_api import async_playwright

URL_PATTERN = re.compile(
    r'https?://[a-z0-9.-]+\.sekai-kabuka\.com/[^\s"\'<>]+'
)
DATA_SIGNATURE = re.compile(r'q\s*\(\s*\d+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*,')

TARGET_PAGE = "https://sekai-kabuka.com/pc-dow30.html"
OUTPUT_URL_FILE = "sekai_data_url.txt"
OUTPUT_INDICES_FILE = "sekai_indices.json"

WAIT_AFTER_LOAD_SEC = 12        # JS 完全レンダリングを待つ
TOTAL_TIMEOUT_SEC = 100
DATA_FETCH_TIMEOUT_SEC = 8

# 抽出したい銘柄のラベル候補 (sekai-kabuka が表示するテキスト)
# key = synthetic_fx.py で使うキー
# value = ページ上で見えそうな日本語ラベル候補のリスト
TARGET_INSTRUMENTS = {
    "dow":      ["サンデーダウ", "ダウ平均", "ダウ"],
    "nas100":   ["サンデーNASDAQ", "NASDAQ100"],
    "oil":      ["サンデー原油", "原油"],
    "gold":     ["サンデーゴールド", "ゴールド", "金先物"],
    "vix":      ["VIX 24時間比", "VIX"],
}


def verify_is_data_url(url: str):
    """URLを叩いて中身にq(...)が含まれるか判定。(is_data, size, snippet)"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "Referer": "https://sekai-kabuka.com/",
        })
        with urllib.request.urlopen(req, timeout=DATA_FETCH_TIMEOUT_SEC) as r:
            raw = r.read()
            ce = r.headers.get("Content-Encoding", "")
        if "gzip" in ce.lower() or raw[:2] == b'\x1f\x8b':
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        try:
            text = raw.decode("shift_jis", errors="ignore")
        except Exception:
            text = raw.decode("ascii", errors="ignore")
        is_data = bool(DATA_SIGNATURE.search(text))
        snippet = text[:80].replace("\n", " ").replace("\r", "")
        return is_data, len(raw), snippet
    except Exception as e:
        return False, 0, f"FETCH_ERROR: {type(e).__name__}: {str(e)[:50]}"


def parse_indices_from_body_text(body_text: str):
    """
    ページ全体のinnerText (body_text) から、各銘柄の現在値を抽出する。

    sekai-kabuka.com の表示は典型的に
        サンデー原油 CFD
        90.53
        -8.06%
    のように label の直後に値が来る縦並びになっている前提。

    返り値: { "oil": 90.53, "dow": 51085.50, ... } (取れたものだけ)
    """
    results = {}
    lines = [ln.strip() for ln in body_text.split("\n") if ln.strip()]

    for key, label_candidates in TARGET_INSTRUMENTS.items():
        found_value = None
        for label in label_candidates:
            # ラベルが含まれる行を探す (完全一致でなく部分一致でOK)
            for i, line in enumerate(lines):
                if label in line:
                    # 直後の数行から最初の「数値らしい行」を探す
                    for j in range(i + 1, min(i + 8, len(lines))):
                        candidate_line = lines[j]
                        # 純粋な数値 ('51,085.50', '90.53', '+0.5%' などを想定)
                        # 「,」「.」を含む数字部分を抜き出して float に
                        m = re.match(
                            r'^\s*([+-]?[\d,]+\.?\d*)\s*$',
                            candidate_line
                        )
                        if m:
                            raw_num = m.group(1).replace(",", "")
                            try:
                                val = float(raw_num)
                            except ValueError:
                                continue
                            # 「%」記号を含むものや極端な値はスキップ
                            if "%" in candidate_line:
                                continue
                            # 数値の妥当性チェック (それぞれの銘柄の典型レンジ)
                            if _is_plausible(key, val):
                                found_value = val
                                break
                    if found_value is not None:
                        break
            if found_value is not None:
                break
        if found_value is not None:
            results[key] = found_value
    return results


def _is_plausible(key: str, val: float) -> bool:
    """各銘柄の典型レンジに収まっているか"""
    ranges = {
        "dow":    (10000, 100000),
        "nas100": (8000,  50000),
        "oil":    (10,    250),
        "gold":   (1000,  10000),
        "vix":    (5,     100),
    }
    lo, hi = ranges.get(key, (None, None))
    if lo is None:
        return True
    return lo <= val <= hi


async def collect_data():
    """
    Playwrightで sekai-kabuka.com を開いて:
      - データURL候補を集める (既存)
      - ★NEW: page.body.innerText を取得 (DOMスクレイプ用)
    """
    captured_urls = []
    seen_urls = set()
    body_text = ""

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                locale="ja-JP",
                viewport={"width": 1280, "height": 800},
            )
            page = await context.new_page()

            def on_request(request):
                url = request.url
                if URL_PATTERN.search(url) and url not in seen_urls:
                    seen_urls.add(url)
                    captured_urls.append(url)

            page.on("request", on_request)
            print(f"[info] Navigating to {TARGET_PAGE}", file=sys.stderr)
            try:
                await page.goto(TARGET_PAGE, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"[warn] goto exception (continuing): {e}", file=sys.stderr)

            print(f"[info] Waiting {WAIT_AFTER_LOAD_SEC}s for JS...", file=sys.stderr)
            await page.wait_for_timeout(WAIT_AFTER_LOAD_SEC * 1000)

            # URL候補をHTMLからも探す
            try:
                html = await page.content()
                for m in URL_PATTERN.finditer(html):
                    u = m.group(0)
                    if u not in seen_urls:
                        seen_urls.add(u)
                        captured_urls.append(u)
            except Exception:
                pass

            # ★NEW: body の innerText を取得
            try:
                body_text = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
            except Exception as e:
                print(f"[warn] body innerText failed: {e}", file=sys.stderr)

        finally:
            await browser.close()

    return captured_urls, body_text


def update_data_url_file(urls):
    """データURL候補から本物を選んで sekai_data_url.txt に書き出す"""
    if not urls:
        return False

    candidates = []
    print(f"\n[info] Verifying {len(urls)} URLs for q() data signature...", file=sys.stderr)
    for u in urls:
        is_data, size, snippet = verify_is_data_url(u)
        marker = "DATA-OK" if is_data else " noise "
        print(f"  [{marker}] size={size:>7}B  {u[:90]}", file=sys.stderr)
        if is_data:
            candidates.append((size, u))

    if not candidates:
        print("[error] No URL returned q() data.", file=sys.stderr)
        return False

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, chosen_url = candidates[0]
    print(f"[info] CHOSEN data URL: {chosen_url}", file=sys.stderr)

    existing_url = ""
    if os.path.exists(OUTPUT_URL_FILE):
        try:
            with open(OUTPUT_URL_FILE, "r", encoding="utf-8") as f:
                existing_url = (f.readline() or "").strip()
        except Exception:
            pass

    if existing_url == chosen_url:
        print(f"[info] data URL unchanged.", file=sys.stderr)
        return True

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(OUTPUT_URL_FILE, "w", encoding="utf-8") as f:
        f.write(f"{chosen_url}\n# updated_at: {now_utc}\n")
    print(f"[info] data URL updated", file=sys.stderr)
    return True


def update_indices_file(body_text):
    """body innerText から各銘柄の現在値を抽出して sekai_indices.json に保存"""
    if not body_text:
        print("[warn] body_text is empty; skipping indices extraction", file=sys.stderr)
        return False

    print(f"\n[info] Parsing indices from body text ({len(body_text)} chars)...", file=sys.stderr)

    # デバッグ用: 最初の500文字を出力 (どんなテキストが取れたか確認)
    snippet = body_text[:500].replace("\n", " | ")
    print(f"[info] Body text snippet: {snippet[:300]}", file=sys.stderr)

    indices = parse_indices_from_body_text(body_text)
    if not indices:
        print("[warn] No instruments matched in body text", file=sys.stderr)
        # サンプルとして "サンデー" を含む行だけダンプしておく
        for line in body_text.split("\n"):
            if "サンデー" in line or "VIX" in line:
                print(f"  candidate line: {line.strip()[:80]}", file=sys.stderr)
        return False

    print(f"[info] Extracted indices: {indices}", file=sys.stderr)

    # 前回と同じならスキップ
    existing = {}
    if os.path.exists(OUTPUT_INDICES_FILE):
        try:
            with open(OUTPUT_INDICES_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
                if not isinstance(existing, dict):
                    existing = {}
        except Exception:
            existing = {}

    existing_values = existing.get("values", {})
    if existing_values == indices:
        print(f"[info] indices unchanged.", file=sys.stderr)
        return True

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    out = {
        "updated_at": now_utc,
        "values": indices,
    }
    with open(OUTPUT_INDICES_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[info] indices updated", file=sys.stderr)
    return True


def main():
    start = time.time()
    try:
        urls, body_text = asyncio.run(
            asyncio.wait_for(collect_data(), timeout=TOTAL_TIMEOUT_SEC)
        )
    except asyncio.TimeoutError:
        print(f"[error] Playwright timeout {TOTAL_TIMEOUT_SEC}s", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[error] Playwright: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"[info] Captured {len(urls)} URLs, body_text={len(body_text)} chars", file=sys.stderr)

    # ① データURL更新
    url_ok = update_data_url_file(urls)

    # ② DOM 現在値更新 (★NEW)
    idx_ok = update_indices_file(body_text)

    elapsed = time.time() - start
    print(f"[info] Done ({elapsed:.1f}s) url_ok={url_ok} indices_ok={idx_ok}", file=sys.stderr)

    # どちらか1つでも成功すれば exit 0
    return 0 if (url_ok or idx_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
