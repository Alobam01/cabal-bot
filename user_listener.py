import re
import math
import time
import aiohttp
import asyncio
from telethon import TelegramClient, events
from telethon.sessions import StringSession

seen_cas_global = {}
training_profile_cache = {}
token_metrics_cache = {}

SCANNER_LINK_RE = re.compile(r"(?:https?://)?t\.me/soul_scanner_bot\?start=([^\s&]+)", re.IGNORECASE)
MULTIPLIER_RE = re.compile(r"\b\d{1,4}\s*x\b", re.IGNORECASE)

def extract_cas_from_scanner_links(text: str):
    if not text:
        return []
    cas = []
    seen = set()
    for match in SCANNER_LINK_RE.finditer(text):
        start_value = (match.group(1) or "").strip()
        if start_value.startswith("ets_"):
            start_value = start_value[len("ets_") :]
        start_value = start_value.strip()
        if not start_value:
            continue
        if start_value in seen:
            continue
        cas.append(start_value)
        seen.add(start_value)
    return cas

def is_multiplier_update(text: str):
    if not text:
        return False
    return bool(MULTIPLIER_RE.search(text))

def _safe_log10(value: float):
    if value <= 0:
        return 0.0
    return math.log10(value)

async def fetch_token_features(http_session: aiohttp.ClientSession, token: str):
    cached = token_metrics_cache.get(token)
    now = time.time()
    if cached and (now - cached["ts"]) < 60:
        return cached["features"]

    url = f"https://api.dexscreener.com/latest/dex/tokens/{token}"
    async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()

    pairs = data.get("pairs") or []
    if not pairs:
        return None

    solana_pairs = [p for p in pairs if (p.get("chainId") or "").lower() == "solana"]
    candidate_pairs = solana_pairs or pairs

    def pair_score(p):
        liquidity_usd = ((p.get("liquidity") or {}).get("usd")) or 0
        vol_h24 = ((p.get("volume") or {}).get("h24")) or 0
        txns_h24 = (p.get("txns") or {}).get("h24") or {}
        tx_count = (txns_h24.get("buys") or 0) + (txns_h24.get("sells") or 0)
        return (liquidity_usd, vol_h24, tx_count)

    pair = max(candidate_pairs, key=pair_score)

    liquidity_usd = float(((pair.get("liquidity") or {}).get("usd")) or 0)
    fdv = float(pair.get("fdv") or 0)
    market_cap = float(pair.get("marketCap") or 0)

    volume = pair.get("volume") or {}
    vol_h24 = float(volume.get("h24") or 0)
    vol_h6 = float(volume.get("h6") or 0)
    vol_h1 = float(volume.get("h1") or 0)

    txns = pair.get("txns") or {}
    txns_h24 = txns.get("h24") or {}
    buys_24h = float(txns_h24.get("buys") or 0)
    sells_24h = float(txns_h24.get("sells") or 0)
    buy_pressure_24h = buys_24h / max(buys_24h + sells_24h, 1.0)

    price_change = pair.get("priceChange") or {}
    pc_h1 = float(price_change.get("h1") or 0)
    pc_h24 = float(price_change.get("h24") or 0)

    features = {
        "liq_log": _safe_log10(liquidity_usd),
        "fdv_log": _safe_log10(fdv),
        "mcap_log": _safe_log10(market_cap),
        "vol_h1_log": _safe_log10(vol_h1),
        "vol_h6_log": _safe_log10(vol_h6),
        "vol_h24_log": _safe_log10(vol_h24),
        "buy_pressure_24h": buy_pressure_24h,
        "pc_h1": pc_h1,
        "pc_h24": pc_h24,
    }

    token_metrics_cache[token] = {"ts": now, "features": features}
    return features

def _build_training_profile(features_list: list[dict]):
    if not features_list:
        return None
    keys = sorted({k for f in features_list for k in f.keys()})
    means = {}
    stds = {}
    for k in keys:
        values = [float(f.get(k, 0.0)) for f in features_list]
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / max(len(values) - 1, 1)
        std = math.sqrt(var) if var > 0 else 1.0
        means[k] = mean
        stds[k] = std
    return {"means": means, "stds": stds, "keys": keys}

def _score_against_profile(token_features: dict, profile: dict):
    keys = profile["keys"]
    means = profile["means"]
    stds = profile["stds"]
    z_abs_sum = 0.0
    count = 0
    for k in keys:
        if k not in token_features:
            continue
        z = (float(token_features[k]) - float(means[k])) / float(stds[k] or 1.0)
        z_abs_sum += abs(z)
        count += 1
    if count == 0:
        return 0.0
    dist = z_abs_sum / count
    return 1.0 / (1.0 + dist)

async def score_token(http_session: aiohttp.ClientSession, token: str, training_cas: list[str]):
    training_cas = [t.strip() for t in (training_cas or []) if isinstance(t, str) and t.strip()]
    training_key = tuple(sorted(set(training_cas)))
    if not training_key:
        return 0.0

    cached_profile = training_profile_cache.get(training_key)
    if not cached_profile:
        training_features = []
        for ca in training_key:
            features = await fetch_token_features(http_session, ca)
            if features:
                training_features.append(features)
        cached_profile = _build_training_profile(training_features)
        training_profile_cache[training_key] = cached_profile

    if not cached_profile:
        return 0.0

    token_features = await fetch_token_features(http_session, token)
    if not token_features:
        return 0.0

    return _score_against_profile(token_features, cached_profile)

async def start_user_listener(user_id: int, session_string: str, source_groups: list, target_group: str, training: list, api_id: int, api_hash: str):
    if not all([session_string, target_group, source_groups]):
        return
    if user_id not in seen_cas_global:
        seen_cas_global[user_id] = set()

    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    http_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=25))
    score_semaphore = asyncio.Semaphore(10)

    @client.on(events.NewMessage(chats=source_groups))
    async def handler(event):
        if not event.message or not event.message.text:
            return
        text = event.message.text.strip()
        if is_multiplier_update(text):
            return
        cas = extract_cas_from_scanner_links(text)
        if not cas:
            return

        async def process_ca(ca: str):
            if ca in seen_cas_global[user_id]:
                return
            async with score_semaphore:
                score = await score_token(http_session, ca, training)
            if score < 0.45:
                return
            try:
                await client.send_message(target_group, ca)
                seen_cas_global[user_id].add(ca)
                print(f"✅ User {user_id} sent CA: {ca[:20]}... score={score:.3f}")
            except Exception as e:
                print(f"Error user {user_id}: {e}")

        await asyncio.gather(*(process_ca(ca) for ca in cas))

    await client.start()
    print(f"🚀 Listener started for user {user_id}")
    try:
        await client.run_until_disconnected()
    finally:
        await http_session.close()
