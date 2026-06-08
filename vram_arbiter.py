import asyncio
import aiohttp
import time
import logging

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger("VRAM_Arbiter")

LM_STUDIO_BASE_URL = "http://host.docker.internal:1234/v1"
MAX_CONCURRENT_MODELS = 1
POLL_INTERVAL_SECONDS = 2.0

class LRUCache:
    def __init__(self, maxsize):
        self.maxsize = maxsize
        self.cache = {}

    def touch(self, key):
        if key in self.cache:
            # Move to end (most recently used)
            value = self.cache.pop(key)
            self.cache[key] = value
        else:
            self.cache[key] = time.time()

    def remove(self, key):
        if key in self.cache:
            del self.cache[key]

    def get_oldest(self):
        if not self.cache:
            return None
        return next(iter(self.cache))

    def __len__(self):
        return len(self.cache)

    def keys(self):
        return list(self.cache.keys())

async def poll_models(session):
    url = f"{LM_STUDIO_BASE_URL}/models"
    try:
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                data = await response.json()
                active_models = [model["id"] for model in data.get("data", [])]
                return active_models
            return []
    except (aiohttp.ClientConnectorError, asyncio.TimeoutError):
        logger.warning(f"[VRAM ARBITER] Connection refused or timed out while polling {url}. LM Studio may still be booting.")
        return []
    except Exception as e:
        logger.error(f"[VRAM ARBITER] Failed to poll LM Studio: {e}")
        return []

async def unload_model(session, model_id):
    logger.warning(f"[VRAM ARBITER] VRAM COLLISION IMMINENT: Evicting model '{model_id}' via native REST API.")
    url = f"{LM_STUDIO_BASE_URL}/models/unload"
    payload = {"instance_id": model_id}
    
    try:
        async with session.post(url, json=payload, timeout=10) as response:
            if response.status == 200:
                logger.info(f"[VRAM ARBITER] Model unloaded via API. Successfully evicted '{model_id}'.")
            else:
                body = await response.text()
                logger.error(f"[VRAM ARBITER] API Eviction failed for '{model_id}': HTTP {response.status} - {body}")
    except (aiohttp.ClientConnectorError, asyncio.TimeoutError):
        logger.error(f"[VRAM ARBITER] Connection error while trying to unload '{model_id}'.")
    except Exception as e:
        logger.error(f"[VRAM ARBITER] Unexpected error during eviction: {e}")

async def arbiter_loop():
    lru_cache = LRUCache(maxsize=MAX_CONCURRENT_MODELS)
    
    # Configure custom TCP connector to optimize keep-alive connections
    connector = aiohttp.TCPConnector(limit=10, force_close=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        logger.info(f"[VRAM ARBITER] Daemon started. Monitoring LM Studio models via HTTP at {LM_STUDIO_BASE_URL}.")
        while True:
            active_models = await poll_models(session)
            
            # Touch active models in LRU
            for model_id in active_models:
                lru_cache.touch(model_id)

            # Remove models from LRU that are no longer active
            for cached_model in lru_cache.keys():
                if cached_model not in active_models:
                    lru_cache.remove(cached_model)

            # Check for breach
            while len(lru_cache) > MAX_CONCURRENT_MODELS:
                oldest_model = lru_cache.get_oldest()
                logger.warning(f"[VRAM ARBITER] Model limit breached! Active models: {len(lru_cache)} > {MAX_CONCURRENT_MODELS}")
                await unload_model(session, oldest_model)
                lru_cache.remove(oldest_model)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    try:
        asyncio.run(arbiter_loop())
    except KeyboardInterrupt:
        logger.info("[VRAM ARBITER] Daemon shutting down.")
