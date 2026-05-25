"""
DYOR Bot — HTTP Webhook Server
Receives CA data from CA Intelligence Bot, runs full analysis
(Playwright scraping + dev wallet history), then forwards enriched
payload to n8n DYOR webhook.
"""

import os
import re
import logging
import asyncio
import aiohttp
from aiohttp import web
from datetime import datetime, timezone
from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
N8N_WEBHOOK  = os.environ["DYOR_WEBHOOK_URL"]
BSCSCAN_KEY  = os.environ.get("BSCSCAN_API_KEY", "")
SOLSCAN_KEY  = os.environ.get("SOLSCAN_API_KEY", "")
PORT         = int(os.environ.get("PORT", 8080))

# ── Playwright Scrapers ───────────────────────────────────────────────────────
async def scrape_twitter(page, url: str) -> dict:
    result = {"twitter_followers": 0, "twitter_age_days": -1,
              "twitter_last_post": "unknown", "twitter_exists": False}
    if not url:
        return result
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(3000)
        content = await page.content()

        followers_match = re.search(r'"followers_count":(\d+)', content)
        if not followers_match:
            followers_match = re.search(r'([\d,]+)\s*Followers', content)
        if followers_match:
            result["twitter_followers"] = int(followers_match.group(1).replace(",", ""))
            result["twitter_exists"] = True

        created_match = re.search(r'Joined\s+(\w+\s+\d{4})', content)
        if created_match:
            try:
                joined = datetime.strptime(created_match.group(1), "%B %Y")
                result["twitter_age_days"] = (datetime.now() - joined).days
            except Exception:
                pass

        time_match = re.search(r'<time[^>]+datetime="([^"]+)"', content)
        if time_match:
            result["twitter_last_post"] = time_match.group(1)[:10]

        log.info(f"🐦 Twitter: {result['twitter_followers']} followers")
    except Exception as e:
        log.warning(f"⚠️ Twitter scrape failed: {e}")
    return result

async def scrape_website(page, url: str) -> dict:
    result = {"website_exists": False, "website_is_template": False, "website_title": ""}
    if not url:
        return result
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        if resp and resp.status < 400:
            result["website_exists"] = True
            content = await page.content()
            result["website_title"] = (await page.title())[:80]
            template_signals = ["squarespace", "wix.com", "webflow", "wordpress",
                                "template", "coming soon", "under construction",
                                "lorem ipsum", "your text here"]
            result["website_is_template"] = sum(1 for s in template_signals if s in content.lower()) >= 2
            log.info(f"🌍 Website: exists=True template={result['website_is_template']}")
    except Exception as e:
        log.warning(f"⚠️ Website scrape failed: {e}")
    return result

async def scrape_telegram(page, url: str) -> dict:
    result = {"telegram_members": 0, "telegram_last_message": "unknown", "telegram_exists": False}
    if not url:
        return result
    try:
        preview_url = url.replace("https://t.me/", "https://t.me/s/") if "/s/" not in url else url
        await page.goto(preview_url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)
        content = await page.content()

        members_match = re.search(r'([\d\s,]+)\s*(?:members|subscribers)', content, re.IGNORECASE)
        if members_match:
            result["telegram_members"] = int(re.sub(r'[^\d]', '', members_match.group(1)))
            result["telegram_exists"] = True

        time_match = re.search(r'<time[^>]+datetime="([^"]+)"', content)
        if time_match:
            result["telegram_last_message"] = time_match.group(1)[:16]

        log.info(f"📱 Telegram: {result['telegram_members']} members")
    except Exception as e:
        log.warning(f"⚠️ Telegram scrape failed: {e}")
    return result

async def scrape_bubblemaps(page, address: str, chain: str) -> dict:
    result = {"bubblemaps_score": -1, "bubblemaps_bundled": False, "bubblemaps_url": ""}
    try:
        chain_map = {"bsc": "bsc", "eth": "eth", "solana": "sol", "base": "base"}
        bm_chain = chain_map.get(chain, "bsc")
        url = f"https://app.bubblemaps.io/{bm_chain}/token/{address}"
        result["bubblemaps_url"] = url
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(5000)
        content = await page.content()

        score_match = re.search(r'"decentralisationScore"\s*:\s*(\d+)', content)
        if not score_match:
            score_match = re.search(r'Decentralisation[^>]*>\s*(\d+)', content)
        if score_match:
            result["bubblemaps_score"] = int(score_match.group(1))
            result["bubblemaps_bundled"] = result["bubblemaps_score"] < 40

        log.info(f"🫧 Bubblemaps: score={result['bubblemaps_score']}")
    except Exception as e:
        log.warning(f"⚠️ Bubblemaps scrape failed: {e}")
    return result

async def run_playwright_scraping(twitter_url, website_url, telegram_url, address, chain) -> dict:
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage", "--disable-gpu"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            page = await context.new_page()
            results = {
                **(await scrape_twitter(page, twitter_url)),
                **(await scrape_website(page, website_url)),
                **(await scrape_telegram(page, telegram_url)),
                **(await scrape_bubblemaps(page, address, chain)),
            }
            await browser.close()
            return results
    except Exception as e:
        log.error(f"❌ Playwright failed: {e}")
        return {
            "twitter_followers": 0, "twitter_age_days": -1,
            "twitter_last_post": "unknown", "twitter_exists": False,
            "website_exists": False, "website_is_template": False,
            "website_title": "", "telegram_members": 0,
            "telegram_last_message": "unknown", "telegram_exists": False,
            "bubblemaps_score": -1, "bubblemaps_bundled": False, "bubblemaps_url": ""
        }

# ── Dev Wallet History ────────────────────────────────────────────────────────
async def get_dev_wallet_history(session: aiohttp.ClientSession, dev_address: str, chain: str) -> dict:
    result = {"dev_prev_tokens": 0, "dev_prev_rugs": 0,
              "dev_wallet_age_days": -1, "dev_rug_history": ""}
    if not dev_address:
        return result
    try:
        if chain == "solana":
            url = f"https://pro-api.solscan.io/v2.0/account/token-accounts?address={dev_address}&type=token"
            async with session.get(url, headers={"token": SOLSCAN_KEY}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
                result["dev_prev_tokens"] = len(data.get("data", []))
        else:
            url = (f"https://api.bscscan.com/api?module=account&action=txlist"
                   f"&address={dev_address}&startblock=0&endblock=99999999"
                   f"&sort=asc&apikey={BSCSCAN_KEY}")
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
                txns = data.get("result", [])
                if isinstance(txns, list):
                    deployments = [t for t in txns if not t.get("to")]
                    result["dev_prev_tokens"] = len(deployments)
                    if txns:
                        first_ts = int(txns[0].get("timeStamp", 0))
                        if first_ts:
                            result["dev_wallet_age_days"] = int(
                                (datetime.now(timezone.utc).timestamp() - first_ts) / 86400)
                    rug_count = 0
                    for deploy in deployments[:10]:
                        contract = deploy.get("contractAddress", "")
                        if not contract:
                            continue
                        check_url = (f"https://api.bscscan.com/api?module=account&action=tokentx"
                                     f"&address={dev_address}&contractaddress={contract}&apikey={BSCSCAN_KEY}")
                        async with session.get(check_url, timeout=aiohttp.ClientTimeout(total=8)) as cr:
                            cdata = await cr.json()
                            ctxns = cdata.get("result", [])
                            if isinstance(ctxns, list):
                                sells = [t for t in ctxns if t.get("from", "").lower() == dev_address.lower()]
                                if sells:
                                    rug_count += 1
                    result["dev_prev_rugs"] = rug_count
                    result["dev_rug_history"] = (
                        f"⚠️ Dev rugged {rug_count} of last {len(deployments[:10])} tokens"
                        if rug_count > 0 else "✅ No obvious rug history found"
                    )
        log.info(f"👛 Dev wallet: {result['dev_prev_tokens']} tokens, {result['dev_prev_rugs']} rugs")
    except Exception as e:
        log.warning(f"⚠️ Dev wallet history failed: {e}")
    return result

# ── Webhook Handler ───────────────────────────────────────────────────────────
async def handle_dyor(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    address = data.get("address", "")
    chain   = data.get("chain", "bsc")
    if chain == "evm":
        chain = "bsc"

    log.info(f"🔬 DYOR triggered: {address} [{chain}]")

    async with aiohttp.ClientSession() as http:
        # 1. Playwright scraping
        playwright_data = await run_playwright_scraping(
            twitter_url  = data.get("twitter_url", ""),
            website_url  = data.get("website_url", ""),
            telegram_url = data.get("telegram_url", ""),
            address      = address,
            chain        = chain
        )

        # 2. GoPlus → dev address → wallet history
        dev_history = {}
        try:
            chain_id = "solana" if chain == "solana" else "56" if chain == "bsc" else "8453" if chain == "base" else "1"
            gp_url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={address}"
            async with http.get(gp_url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                gp_data = await r.json()
                gp_result = gp_data.get("result", {})
                token_data = list(gp_result.values())[0] if gp_result else {}
                dev_address = token_data.get("creator_address", "")
                if dev_address:
                    dev_history = await get_dev_wallet_history(http, dev_address, chain)
        except Exception as e:
            log.warning(f"⚠️ GoPlus failed: {e}")

        # 3. Forward enriched payload to n8n
        payload = {**data, **playwright_data, **dev_history}
        try:
            async with http.post(N8N_WEBHOOK, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    log.info(f"✅ Enriched payload sent to n8n: {address}")
                else:
                    log.warning(f"⚠️ n8n returned {resp.status}")
        except Exception as e:
            log.error(f"❌ Failed to send to n8n: {e}")

    return web.Response(status=200, text="ok")

async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="DYOR bot healthy")

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    app = web.Application()
    app.router.add_post("/dyor", handle_dyor)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"🚀 DYOR bot listening on port {PORT}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
