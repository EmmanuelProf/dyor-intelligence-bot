"""
DYOR Bot — HTTP Webhook Server
Receives CA data, runs Playwright scraping + dev wallet analysis,
forwards enriched payload to n8n DYOR webhook.
"""

import os
import re
import logging
import asyncio
import aiohttp
from aiohttp import web
from datetime import datetime, timezone
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

N8N_WEBHOOK = os.environ["DYOR_WEBHOOK_URL"]
BSCSCAN_KEY = os.environ.get("BSCSCAN_API_KEY", "")
SOLSCAN_KEY = os.environ.get("SOLSCAN_API_KEY", "")
PORT        = int(os.environ.get("PORT", 8080))

# ── Playwright Scrapers ───────────────────────────────────────────────────────
async def scrape_twitter(page, url: str) -> dict:
    r = {"twitter_followers": 0, "twitter_age_days": -1, "twitter_last_post": "", "twitter_exists": False}
    if not url:
        return r
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(3000)
        c = await page.content()
        m = re.search(r'"followers_count":(\d+)', c) or re.search(r'([\d,]+)\s*Followers', c)
        if m:
            r["twitter_followers"] = int(m.group(1).replace(",", ""))
            r["twitter_exists"] = True
        m2 = re.search(r'Joined\s+(\w+\s+\d{4})', c)
        if m2:
            try:
                r["twitter_age_days"] = (datetime.now() - datetime.strptime(m2.group(1), "%B %Y")).days
            except Exception:
                pass
        m3 = re.search(r'<time[^>]+datetime="([^"]+)"', c)
        if m3:
            r["twitter_last_post"] = m3.group(1)[:10]
        log.info(f"🐦 Twitter: {r['twitter_followers']} followers")
    except Exception as e:
        log.warning(f"⚠️ Twitter failed: {e}")
    return r

async def scrape_website(page, url: str) -> dict:
    r = {"website_exists": False, "website_is_template": False, "website_title": ""}
    if not url:
        return r
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        if resp and resp.status < 400:
            r["website_exists"] = True
            c = await page.content()
            r["website_title"] = (await page.title())[:80]
            signals = ["squarespace","wix.com","webflow","wordpress","template","coming soon","lorem ipsum"]
            r["website_is_template"] = sum(1 for s in signals if s in c.lower()) >= 2
        log.info(f"🌍 Website: {r['website_exists']}")
    except Exception as e:
        log.warning(f"⚠️ Website failed: {e}")
    return r

async def scrape_telegram(page, url: str) -> dict:
    r = {"telegram_members": 0, "telegram_exists": False}
    if not url:
        return r
    try:
        preview = url.replace("https://t.me/", "https://t.me/s/") if "/s/" not in url else url
        await page.goto(preview, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)
        c = await page.content()
        m = re.search(r'([\d\s,]+)\s*(?:members|subscribers)', c, re.IGNORECASE)
        if m:
            r["telegram_members"] = int(re.sub(r'[^\d]', '', m.group(1)))
            r["telegram_exists"] = True
        log.info(f"📱 Telegram: {r['telegram_members']} members")
    except Exception as e:
        log.warning(f"⚠️ Telegram failed: {e}")
    return r

async def scrape_bubblemaps(page, address: str, chain: str) -> dict:
    r = {"bubblemaps_score": -1, "bubblemaps_bundled": False, "bubblemaps_url": ""}
    try:
        cm = {"bsc": "bsc", "eth": "eth", "solana": "sol", "base": "base"}
        url = f"https://app.bubblemaps.io/{cm.get(chain,'bsc')}/token/{address}"
        r["bubblemaps_url"] = url
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(5000)
        c = await page.content()
        m = re.search(r'"decentralisationScore"\s*:\s*(\d+)', c) or re.search(r'Decentralisation[^>]*>\s*(\d+)', c)
        if m:
            r["bubblemaps_score"] = int(m.group(1))
            r["bubblemaps_bundled"] = r["bubblemaps_score"] < 40
        log.info(f"🫧 Bubblemaps: score={r['bubblemaps_score']}")
    except Exception as e:
        log.warning(f"⚠️ Bubblemaps failed: {e}")
    return r

CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH", "")

async def run_scraping(twitter_url, website_url, telegram_url, address, chain) -> dict:
    try:
        async with async_playwright() as pw:
            launch_args = dict(
                headless=True,
                args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu"]
            )
            if CHROMIUM_PATH:
                launch_args["executable_path"] = CHROMIUM_PATH
            browser = await pw.chromium.launch(**launch_args)
            page = await (await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )).new_page()
            results = {
                **(await scrape_twitter(page, twitter_url)),
                **(await scrape_website(page, website_url)),
                **(await scrape_telegram(page, telegram_url)),
                **(await scrape_bubblemaps(page, address, chain)),
            }
            await browser.close()
            return results
    except Exception as e:
        log.error(f"❌ Playwright error: {e}")
        return {"twitter_followers":0,"twitter_age_days":-1,"twitter_last_post":"","twitter_exists":False,
                "website_exists":False,"website_is_template":False,"website_title":"",
                "telegram_members":0,"telegram_exists":False,
                "bubblemaps_score":-1,"bubblemaps_bundled":False,"bubblemaps_url":""}

# ── GoPlus + Dev Wallet ───────────────────────────────────────────────────────
async def get_goplus(session, address, chain) -> dict:
    r = {"dev_address":"","is_honeypot":False,"buy_tax":0,"sell_tax":0,"is_open_source":False}
    try:
        cid = "solana" if chain=="solana" else "56" if chain=="bsc" else "8453" if chain=="base" else "1"
        async with session.get(f"https://api.gopluslabs.io/api/v1/token_security/{cid}?contract_addresses={address}",
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            vals = list((data.get("result") or {}).values())
            if vals:
                d = vals[0]
                r["dev_address"]   = d.get("creator_address","")
                r["is_honeypot"]   = d.get("is_honeypot","0") == "1"
                r["is_open_source"]= d.get("is_open_source","0") == "1"
                r["buy_tax"]       = float(d.get("buy_tax",0) or 0)
                r["sell_tax"]      = float(d.get("sell_tax",0) or 0)
        log.info(f"🔐 GoPlus: honeypot={r['is_honeypot']} buy={r['buy_tax']} sell={r['sell_tax']}")
    except Exception as e:
        log.warning(f"⚠️ GoPlus failed: {e}")
    return r

async def get_dev_history(session, dev_address, chain) -> dict:
    r = {"dev_prev_tokens":0,"dev_prev_rugs":0,"dev_wallet_age_days":-1,"dev_rug_history":""}
    if not dev_address:
        return r
    try:
        if chain == "solana":
            async with session.get(f"https://pro-api.solscan.io/v2.0/account/token-accounts?address={dev_address}&type=token",
                                   headers={"token": SOLSCAN_KEY}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                r["dev_prev_tokens"] = len(data.get("data",[]))
        else:
            async with session.get(f"https://api.bscscan.com/api?module=account&action=txlist&address={dev_address}&startblock=0&endblock=99999999&sort=asc&apikey={BSCSCAN_KEY}",
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                txns = data.get("result",[])
                if isinstance(txns, list):
                    deploys = [t for t in txns if not t.get("to")]
                    r["dev_prev_tokens"] = len(deploys)
                    if txns:
                        ts = int(txns[0].get("timeStamp",0))
                        if ts:
                            r["dev_wallet_age_days"] = int((datetime.now(timezone.utc).timestamp()-ts)/86400)
                    rugs = 0
                    for dep in deploys[:10]:
                        contract = dep.get("contractAddress","")
                        if not contract:
                            continue
                        async with session.get(f"https://api.bscscan.com/api?module=account&action=tokentx&address={dev_address}&contractaddress={contract}&apikey={BSCSCAN_KEY}",
                                               timeout=aiohttp.ClientTimeout(total=8)) as cr:
                            cd = await cr.json()
                            sells = [t for t in cd.get("result",[]) if t.get("from","").lower()==dev_address.lower()]
                            if sells:
                                rugs += 1
                    r["dev_prev_rugs"] = rugs
                    r["dev_rug_history"] = f"⚠️ Rugged {rugs}/{len(deploys[:10])} tokens" if rugs else "✅ No obvious rug history"
        log.info(f"👛 Dev: {r['dev_prev_tokens']} tokens, {r['dev_prev_rugs']} rugs")
    except Exception as e:
        log.warning(f"⚠️ Dev history failed: {e}")
    return r

# ── Request Handler ───────────────────────────────────────────────────────────
async def handle_dyor(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    address = data.get("address","")
    chain   = data.get("chain","bsc")
    if chain == "evm":
        chain = "bsc"
    log.info(f"🔬 DYOR: {address} [{chain}]")

    async with aiohttp.ClientSession() as http:
        scrape   = await run_scraping(data.get("twitter_url",""), data.get("website_url",""),
                                       data.get("telegram_url",""), address, chain)
        goplus   = await get_goplus(http, address, chain)
        dev_hist = await get_dev_history(http, goplus.get("dev_address",""), chain)
        payload  = {**data, **scrape, **goplus, **dev_hist}
        try:
            async with http.post(N8N_WEBHOOK, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                log.info(f"{'✅' if resp.status==200 else '⚠️'} n8n response: {resp.status}")
        except Exception as e:
            log.error(f"❌ n8n send failed: {e}")

    return web.Response(status=200, text="ok")

async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")

# ── App ───────────────────────────────────────────────────────────────────────
app = web.Application()
app.router.add_post("/dyor", handle_dyor)
app.router.add_get("/health", handle_health)

if __name__ == "__main__":
    log.info(f"🚀 DYOR bot starting on port {PORT}")
    web.run_app(app, host="0.0.0.0", port=PORT)
