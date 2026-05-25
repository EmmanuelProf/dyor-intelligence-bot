"""
DYOR Bot — HTTP Webhook Server
Receives CA data from CA Intel Bot, enriches it with:
  - GoPlus security data + dev address
  - Dev wallet history (BSCScan / Solscan)
  - Bubblemaps URL
  - Basic website/Telegram check via HTTP
Then forwards enriched payload to n8n DYOR webhook.
"""

import os
import re
import logging
import asyncio
import aiohttp
from aiohttp import web
from datetime import datetime, timezone

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

# ── GoPlus Security ───────────────────────────────────────────────────────────
async def get_goplus_data(session: aiohttp.ClientSession, address: str, chain: str) -> dict:
    result = {"dev_address": "", "is_honeypot": False, "buy_tax": 0,
              "sell_tax": 0, "is_open_source": False, "owner_address": ""}
    try:
        chain_id = "solana" if chain == "solana" else "56" if chain == "bsc" else "8453" if chain == "base" else "1"
        url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={address}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            gp = list(data.get("result", {}).values())
            if gp:
                d = gp[0]
                result["dev_address"]    = d.get("creator_address", "")
                result["owner_address"]  = d.get("owner_address", "")
                result["is_honeypot"]    = d.get("is_honeypot", "0") == "1"
                result["is_open_source"] = d.get("is_open_source", "0") == "1"
                try:
                    result["buy_tax"]  = float(d.get("buy_tax", 0) or 0)
                    result["sell_tax"] = float(d.get("sell_tax", 0) or 0)
                except Exception:
                    pass
        log.info(f"🔐 GoPlus: honeypot={result['is_honeypot']} buy_tax={result['buy_tax']} sell_tax={result['sell_tax']}")
    except Exception as e:
        log.warning(f"⚠️ GoPlus failed: {e}")
    return result

# ── Dev Wallet History ────────────────────────────────────────────────────────
async def get_dev_wallet_history(session: aiohttp.ClientSession, dev_address: str, chain: str) -> dict:
    result = {"dev_prev_tokens": 0, "dev_prev_rugs": 0,
              "dev_wallet_age_days": -1, "dev_rug_history": "unknown"}
    if not dev_address:
        return result
    try:
        if chain == "solana":
            url = f"https://pro-api.solscan.io/v2.0/account/token-accounts?address={dev_address}&type=token"
            async with session.get(url, headers={"token": SOLSCAN_KEY},
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
                result["dev_prev_tokens"] = len(data.get("data", []))
                result["dev_rug_history"] = "✅ Solscan data fetched"
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
                                sells = [t for t in ctxns
                                         if t.get("from", "").lower() == dev_address.lower()]
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

# ── Basic HTTP Checks ─────────────────────────────────────────────────────────
async def check_website(session: aiohttp.ClientSession, url: str) -> dict:
    result = {"website_exists": False, "website_title": ""}
    if not url:
        return result
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8),
                               allow_redirects=True) as r:
            if r.status < 400:
                result["website_exists"] = True
                text = await r.text()
                title = re.search(r'<title[^>]*>([^<]+)</title>', text, re.IGNORECASE)
                if title:
                    result["website_title"] = title.group(1).strip()[:80]
        log.info(f"🌍 Website: exists={result['website_exists']}")
    except Exception as e:
        log.warning(f"⚠️ Website check failed: {e}")
    return result

async def check_telegram(session: aiohttp.ClientSession, url: str) -> dict:
    result = {"telegram_members": 0, "telegram_exists": False}
    if not url:
        return result
    try:
        preview = url.replace("https://t.me/", "https://t.me/s/") if "/s/" not in url else url
        async with session.get(preview, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status < 400:
                text = await r.text()
                m = re.search(r'([\d\s,]+)\s*(?:members|subscribers)', text, re.IGNORECASE)
                if m:
                    result["telegram_members"] = int(re.sub(r'[^\d]', '', m.group(1)))
                    result["telegram_exists"] = True
        log.info(f"📱 Telegram: {result['telegram_members']} members")
    except Exception as e:
        log.warning(f"⚠️ Telegram check failed: {e}")
    return result

# ── Bubblemaps URL Builder ────────────────────────────────────────────────────
def get_bubblemaps_url(address: str, chain: str) -> str:
    chain_map = {"bsc": "bsc", "eth": "eth", "solana": "sol", "base": "base"}
    return f"https://app.bubblemaps.io/{chain_map.get(chain, 'bsc')}/token/{address}"

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
        # 1. GoPlus security + dev address
        goplus = await get_goplus_data(http, address, chain)

        # 2. Dev wallet history
        dev_history = {}
        if goplus.get("dev_address"):
            dev_history = await get_dev_wallet_history(http, goplus["dev_address"], chain)

        # 3. Website + Telegram checks
        website_data = await check_website(http, data.get("website_url", ""))
        telegram_data = await check_telegram(http, data.get("telegram_url", ""))

        # 4. Build enriched payload
        payload = {
            **data,
            **goplus,
            **dev_history,
            **website_data,
            **telegram_data,
            "bubblemaps_url": get_bubblemaps_url(address, chain),
        }

        # 5. Forward to n8n
        try:
            async with http.post(N8N_WEBHOOK, json=payload,
                                 timeout=aiohttp.ClientTimeout(total=15)) as resp:
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
