import aiohttp

async def get_sol_price_usd():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd") as resp:
                data = await resp.json()
                return float(data.get("solana", {}).get("usd", 80.0))
    except:
        return 80.0