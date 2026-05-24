"""
refresh_sekai_url.py

GitHub Actions から定期的に呼ばれて、sekai-kabuka.com の最新データURLを
Playwright (本物のChromiumブラウザ) で取得し、sekai_data_url.txt に書き出す。

通常の requests では JS が実行されず URL が取れないため、ヘッドレスブラウザが必須。

出力:
  sekai_data_url.txt  ... 取得した URL (1行) + UTC timestamp

Exit code:
  0: URL取得成功 (ファイル更新)
  1: URL取得失敗
"""

import asyncio
import os
import re
import sys
import time
from datetime import datetime, timezone

from playwright.async_api import async_playwright

# 目標URLのパターン: 49-212-X-Y.sekai-kabuka.com/-.{token}.{hash}.1.js
URL_PATTERN = re.compile(
    r'https?://[a-z0-9-]+\.sekai-kabuka\.com/[^\s"\'<>?]+\.1\.js'
)

TARGET_PAGE = "https://sekai-kabuka.com/pc-dow30.html"
OUTPUT_FILE = "sekai_data_url.txt"

# ページロード後の待機時間 (JSが動いて URL を生成するのを待つ)
WAIT_AFTER_LOAD_SEC = 8

# 全体タイムアウト
TOTAL_TIMEOUT_SEC = 60


async def fetch_url() -> str:
    """sekai-kabuka.com を Playwright で開き、データURLを返す。失敗時は空文字。"""
    captured = []  # 取得した URL のリスト (複数捕捉してから一番大きそうなのを選ぶ)

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

            # ネットワークリクエストを監視
            def on_request(request):
                url = request.url
                if URL_PATTERN.search(url):
                    captured.append(url)
                    print(f"[capture] {url}", file=sys.stderr)

            page.on("request", on_request)

            print(f"[info] Navigating to {TARGET_PAGE}", file=sys.stderr)
            try:
                await page.goto(TARGET_PAGE, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"[warn] goto exception (continuing): {e}", file=sys.stderr)

            # JS が動いて URL を生成するまで待機
            print(f"[info] Waiting {WAIT_AFTER_LOAD_SEC}s for JS to generate URLs...",
                  file=sys.stderr)
            await page.wait_for_timeout(WAIT_AFTER_LOAD_SEC * 1000)

            # ページのHTMLからも一応探す
            try:
                html = await page.content()
                m = URL_PATTERN.search(html)
                if m:
                    captured.append(m.group(0))
                    print(f"[capture from HTML] {m.group(0)}", file=sys.stderr)
            except Exception:
                pass

        finally:
            await browser.close()

    if not captured:
        return ""

    # 重複を除去しつつ初回検出順を保持
    seen = set()
    unique_urls = []
    for u in captured:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    # 最初に検出されたものを採用 (= ページが最初に読み込んだ本データファイル)
    chosen = unique_urls[0]
    print(f"[info] Captured {len(unique_urls)} unique URLs, "
          f"using first: {chosen}", file=sys.stderr)
    return chosen


def main() -> int:
    start = time.time()
    try:
        url = asyncio.run(asyncio.wait_for(fetch_url(), timeout=TOTAL_TIMEOUT_SEC))
    except asyncio.TimeoutError:
        print(f"[error] Timeout after {TOTAL_TIMEOUT_SEC}s", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[error] Unexpected: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    elapsed = time.time() - start
    if not url:
        print(f"[error] No URL found (took {elapsed:.1f}s)", file=sys.stderr)
        return 1

    # 既存ファイルと比較
    existing_url = ""
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                first_line = (f.readline() or "").strip()
                existing_url = first_line
        except Exception:
            pass

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    new_content = f"{url}\n# updated_at: {now_utc}\n"

    # 内容が同じならファイルを書き換えない (= git commit が発生しない)
    if existing_url == url:
        print(f"[info] URL unchanged. (took {elapsed:.1f}s)", file=sys.stderr)
        # タイムスタンプだけは更新したいが、コミットを増やしたくないので書かない
        return 0

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(new_content)
    print(f"[info] URL updated. (took {elapsed:.1f}s)", file=sys.stderr)
    print(f"[info] OLD: {existing_url}", file=sys.stderr)
    print(f"[info] NEW: {url}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
