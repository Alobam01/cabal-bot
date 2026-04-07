import re
from difflib import SequenceMatcher
from telethon import TelegramClient, events
from telethon.sessions import StringSession

seen_cas_global = {}

def extract_solana_ca(text: str):
    pattern = r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b'
    matches = re.findall(pattern, text)
    return max(matches, key=len) if matches else None

def is_high_quality_cabal_signal(text: str, training_examples: list, threshold=0.50):
    if not text or len(text.strip()) < 25:
        return False
    text_lower = text.lower()
    keywords = ["cabal", "launch", "gem", "alpha", "snipers", "early", "100x", "dev", "lp", "burn", "chart", "dexscreener", "pump.fun"]
    if sum(kw in text_lower for kw in keywords) < 2:
        return False
    for ex in training_examples:
        if SequenceMatcher(None, text_lower, ex.lower()).ratio() >= threshold:
            return True
    return False

async def start_user_listener(user_id: int, session_string: str, source_groups: list, target_group: str, training: list, api_id: int, api_hash: str):
    if not all([session_string, target_group, source_groups]):
        return
    if user_id not in seen_cas_global:
        seen_cas_global[user_id] = set()

    client = TelegramClient(StringSession(session_string), api_id, api_hash)

    @client.on(events.NewMessage(chats=source_groups))
    async def handler(event):
        if not event.message or not event.message.text:
            return
        text = event.message.text.strip()
        if not is_high_quality_cabal_signal(text, training):
            return
        ca = extract_solana_ca(text)
        if not ca or ca in seen_cas_global[user_id]:
            return
        try:
            await client.send_message(target_group, ca)
            seen_cas_global[user_id].add(ca)
            print(f"✅ User {user_id} sent CA: {ca[:20]}...")
        except Exception as e:
            print(f"Error user {user_id}: {e}")

    await client.start()
    print(f"🚀 Listener started for user {user_id}")
    await client.run_until_disconnected()