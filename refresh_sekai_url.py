"""
refresh_sekai_url.py (v2)

GitHub Actions から定期的に呼ばれて、sekai-kabuka.com の最新データURLを
Playwright (本物のChromiumブラウザ) で取得し、sekai_data_url.txt に書き出す。

v2 改善点:
  - URLパターンに当てはまるリクエストを複数キャプチャ
  - 各URLを実際に叩いて q(...) パターンを含むものだけ「本物のデータURL」として採用
  - ログを充実 (どのURLがマッチして、どれがデータだったかが見える)

Exit code:
  0: URL取得成功
  1: URL取得失敗
"""

import asyncio
import os
import re
import sys
import time
import gzip
from datetime import datetime, timezone

import urllib.request
from playwright.async_api import async_playwright

# sekai-kabuka.com のドメイン配下、何かしらのファイルを広くキャプチャ
URL_PATTERN = re.compile(
    r'https?://[a-z0-9.-]+\.sekai-kabuka\.com/[^\s"\'<>]+'
)

# データファイルかどうかの判定: 中身に q(数字,数字,...) パターンが含まれるか
DATA_SIGNATURE = re.compile(r'q\s*\(\s*\d+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*,')

TARGET_PAGE = "https://sekai-kabuka.com/pc-dow30.html"
OUTPUT_FILE = "sekai_data_url.txt"

WAIT_AFTER_LOAD_SEC = 10
TOTAL_TIMEOUT_SEC = 90
DATA_FETCH_TIMEOUT_SEC = 8


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


async def collect_urls():
    captured = []
    seen = set()
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
                if URL_PATTERN.search(url) and url not in seen:
                    seen.add(url)
                    captured.append(url)

            page.on("request", on_request)
            print(f"[info] Navigating to {TARGET_PAGE}", file=sys.stderr)
            try:
                await page.goto(TARGET_PAGE, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"[warn] goto exception (continuing): {e}", file=sys.stderr)

            print(f"[info] Waiting {WAIT_AFTER_LOAD_SEC}s for JS...", file=sys.stderr)
            await page.wait_for_timeout(WAIT_AFTER_LOAD_SEC * 1000)

            try:
                html = await page.content()
                for m in URL_PATTERN.finditer(html):
                    u = m.group(0)
                    if u not in seen:
                        seen.add(u)
                        captured.append(u)
            except Exception:
                pass
        finally:
            await browser.close()
    return captured


def main():
    start = time.time()
    try:
        urls = asyncio.run(asyncio.wait_for(collect_urls(), timeout=TOTAL_TIMEOUT_SEC))
    except asyncio.TimeoutError:
        print(f"[error] Playwright timeout {TOTAL_TIMEOUT_SEC}s", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[error] Playwright: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"[info] Captured {len(urls)} URLs from sekai-kabuka.com", file=sys.stderr)
    for i, u in enumerate(urls):
        print(f"  [{i}] {u[:120]}", file=sys.stderr)
    if not urls:
        print("[error] No URLs captured.", file=sys.stderr)
        return 1

    candidates = []
    print(f"\n[info] Verifying each URL for q() data signature...", file=sys.stderr)
    for u in urls:
        is_data, size, snippet = verify_is_data_url(u)
        marker = "DATA-OK" if is_data else " noise "
        print(f"  [{marker}] size={size:>7}B  {u[:90]}", file=sys.stderr)
        print(f"             snippet: {snippet[:80]}", file=sys.stderr)
        if is_data:
            candidates.append((size, u))

    if not candidates:
        print("\n[error] No URL returned q() data.", file=sys.stderr)
        return 1

    candidates.sort(key=lambda x: x[0], reverse=True)
    chosen_size, chosen_url = candidates[0]
    print(f"\n[info] CHOSEN: size={chosen_size}B  {chosen_url}", file=sys.stderr)

    existing_url = ""
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                existing_url = (f.readline() or "").strip()
        except Exception:
            pass

    elapsed = time.time() - start
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if existing_url == chosen_url:
        print(f"[info] URL unchanged ({elapsed:.1f}s)", file=sys.stderr)
        return 0

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(f"{chosen_url}\n# updated_at: {now_utc}\n")
    print(f"[info] URL updated ({elapsed:.1f}s)", file=sys.stderr)
    print(f"[info] OLD: {existing_url}", file=sys.stderr)
    print(f"[info] NEW: {chosen_url}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
