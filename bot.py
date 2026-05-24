"""
DYOR Bot — Telegram @LETSMAKE_M Listener
Watches @LETSMAKE_M for CA alerts posted by CA Intelligence bot
and forwards them to n8n DYOR webhook for full analysis.
"""

import os
import re
import logging
import asyncio
import aiohttp
from datetime import datetime
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

API_ID       = int(os.environ["TELEGRAM_API_ID"])
API_HASH     = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PHONE_NUMBER = os.environ.get("TELEGRAM_PHONE", "")
SESSION_STR  = os.environ.get("TELEGRAM_SESSION", "")
N8N_WEBHOOK  = os.environ["DYOR_WEBHOOK_URL"]  # /webhook/dyor-intel

# Only watch LETSMAKE_M — this is where CA Intelligence posts alerts
WATCH_CHANNEL = os.environ.get("DYOR_SOURCE_CHANNEL", "LETSMAKE_M")

# CA Pattern Detection
EVM_PATTERN = re.compile(r'\b(0x[a-fA-F0-9]{40})\b')
TRX_PATTERN = re.compile(r'\b(T[1-9A-HJ-NP-Za-km-z]{33})\b')
SOL_PATTERN = re.compile(r'\b([1-9A-HJ-NP-Za-km-z]{43,44})\b')

SOL_NOISE = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
}

def extract_cas(text: str) -> list[dict]:
    found = []
    seen  = set()

    for match in EVM_PATTERN.finditer(text):
        addr = match.group(1).lower()
        if addr not in seen:
            seen.add(addr)
            found.append({"address": addr, "chain": "evm"})

    for match in TRX_PATTERN.finditer(text):
        addr = match.group(1)
        if addr not in seen:
            seen.add(addr)
            found.append({"address": addr, "chain": "tron"})

    if not found:
        for match in SOL_PATTERN.finditer(text):
            addr = match.group(1)
            if addr not in seen and addr not in SOL_NOISE:
                seen.add(addr)
                found.append({"address": addr, "chain": "solana"})

    return found

def extract_metadata(text: str) -> dict:
    """
    Extract token name, symbol, chain, caller info
    already parsed from the CA Intelligence alert message format.
    """
    meta = {
        "token_name":   "",
        "token_symbol": "",
        "chain":        "",
        "caller":       "",
        "channel":      "",
        "dex_url":      ""
    }

    # Extract token name from bold header e.g. "Token Name (SYMBOL)"
    name_match = re.search(r'🔍 CA INTELLIGENCE REPORT\s+(.+?)\s+\((.+?)\)', text)
    if name_match:
        meta["token_name"]   = name_match.group(1).strip()
        meta["token_symbol"] = name_match.group(2).strip()

    # Extract chain
    chain_match = re.search(r'Chain:\s*@?(\w+)', text)
    if chain_match:
        meta["chain"] = chain_match.group(1).lower()

    # Extract caller
    caller_match = re.search(r'Caller:\s*(\S+)', text)
    if caller_match:
        meta["caller"] = caller_match.group(1)

    # Extract channel
    channel_match = re.search(r'Channel:\s*@?(\S+)', text)
    if channel_match:
        meta["channel"] = channel_match.group(1)

    # Extract DexScreener URL
    dex_match = re.search(r'https://dexscreener\.com/\S+', text)
    if dex_match:
        meta["dex_url"] = dex_match.group(0).rstrip('>')

    return meta

async def send_to_n8n(session: aiohttp.ClientSession, payload: dict):
    try:
        async with session.post(
            N8N_WEBHOOK,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status == 200:
                log.info(f"✅ Sent to DYOR webhook: {payload['address']}")
            else:
                body = await resp.text()
                log.warning(f"⚠️  n8n {resp.status}: {body[:100]}")
    except asyncio.TimeoutError:
        log.error(f"❌ Timeout: {payload['address']}")
    except Exception as e:
        log.error(f"❌ Error: {e}")

def build_client() -> TelegramClient:
    if SESSION_STR:
        log.info("🔑 Auth: USER (StringSession)")
        return TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
    if PHONE_NUMBER:
        log.info("🔑 Auth: USER (phone)")
        return TelegramClient(StringSession(), API_ID, API_HASH)
    log.info("🔑 Auth: BOT TOKEN")
    return TelegramClient(StringSession(), API_ID, API_HASH)

async def main():
    log.info(f"DYOR Bot starting — watching @{WATCH_CHANNEL}")

    client = build_client()

    if SESSION_STR:
        await client.start()
    elif PHONE_NUMBER:
        await client.start(phone=PHONE_NUMBER)
        session_string = client.session.save()
        log.info("=" * 60)
        log.info("SAVE THIS TO TELEGRAM_SESSION env var:")
        log.info(session_string)
        log.info("=" * 60)
    else:
        await client.start(bot_token=BOT_TOKEN)

    me = await client.get_me()
    log.info(f"Logged in as: {getattr(me, 'username', None) or getattr(me, 'first_name', 'unknown')}")

    try:
        entity = await client.get_entity(WATCH_CHANNEL)
        log.info(f"✅ Monitoring: @{WATCH_CHANNEL}")
    except Exception as e:
        log.error(f"❌ Could not resolve @{WATCH_CHANNEL}: {e}")
        return

    async with aiohttp.ClientSession() as http:

        @client.on(events.NewMessage(chats=[entity]))
        async def handler(event):
            msg  = event.message
            text = msg.message or ""
            if not text:
                return

            # Only process messages that contain CA Intelligence reports
            # i.e. messages that have a CA address in them
            cas = extract_cas(text)
            if not cas:
                return

            meta = extract_metadata(text)

            for ca in cas:
                # Merge chain from metadata if available
                chain = meta.get("chain") or ca["chain"]
                if chain == "evm":
                    chain = "bsc"

                payload = {
                    "address":      ca["address"],
                    "chain":        chain,
                    "token_name":   meta.get("token_name", ""),
                    "token_symbol": meta.get("token_symbol", ""),
                    "caller":       meta.get("caller", "unknown"),
                    "channel":      meta.get("channel", "unknown"),
                    "dex_url":      meta.get("dex_url", ""),
                    "raw_message":  text[:500],
                    "timestamp":    datetime.utcnow().isoformat(),
                    "message_link": f"https://t.me/{WATCH_CHANNEL}/{msg.id}"
                }
                log.info(f"🔬 DYOR triggered: {ca['address']} [{chain}]")
                await send_to_n8n(http, payload)

        log.info("👂 Listening for CA alerts on @LETSMAKE_M...")
        await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
