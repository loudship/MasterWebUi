import asyncio
import time
import aiohttp
import logging
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "ops"))
from swarm_orchestrator import ContextFirewall, Pipeline

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger("Swarm_Stress_Test")

# Configurations
LM_STUDIO_URL = "http://localhost:4321/v1/chat/completions"

async def vram_collision_injection():
    logger.info("=== STARTING VRAM COLLISION INJECTION ===")
    
    # We will rapidly request 3 different models to force a VRAM overflow.
    # We expect vram_arbiter.py (running in background) to intercept and evict.
    models = ["model-a:latest", "model-b:latest", "model-c:latest"]
    
    async def request_model(session, model_id):
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "Hello!"}],
            "max_tokens": 10
        }
        logger.info(f"Requesting load for {model_id}...")
        start_t = time.time()
        try:
            async with session.post(LM_STUDIO_URL, json=payload, timeout=5) as response:
                await response.json()
        except Exception as e:
            # We expect these might timeout or fail if LM studio is a dummy, but the request goes out.
            pass
        logger.info(f"Request for {model_id} completed in {(time.time() - start_t):.2f}s")

    async with aiohttp.ClientSession() as session:
        # Fire requests concurrently
        tasks = [request_model(session, m) for m in models]
        await asyncio.gather(*tasks)
        
    logger.info("VRAM Collision Injection sent. Check arbiter logs for eviction ms timing.")

def payload_truncation_test():
    logger.info("=== STARTING PAYLOAD TRUNCATION TEST ===")
    
    firewall = ContextFirewall(max_length=20000)
    
    # Generate 50,000 characters packed with faux-Base64 AND >20,000 chars of normal text
    base64_blob = "data:image/png;base64," + ("A" * 1000) + "=="
    faux_text = "Some normal text that takes up space. " * 50 + base64_blob + " "
    
    # Repeat to ensure text length alone exceeds 20k even after stripping base64
    large_payload = faux_text * 50
    
    logger.info(f"Original payload size: {len(large_payload)} characters.")
    
    sanitized = firewall.sanitize(large_payload)
    
    logger.info(f"Sanitized payload size: {len(sanitized)} characters.")
    
    # Assertions
    if "data:image/png;base64," in sanitized:
        logger.error("Base64 string was not stripped!")
    else:
        logger.info("[SUCCESS] Base64 strings successfully stripped.")
        
    if "[WARNING: Payload truncated to preserve VRAM]" in sanitized:
        logger.info("[SUCCESS] Truncation warning successfully appended.")
    else:
        logger.error("Truncation warning missing!")
        
    if len(sanitized) > 20100: # 20000 + length of warning message
        logger.error(f"Payload not truncated properly. Size is {len(sanitized)}.")
    else:
        logger.info("[SUCCESS] Payload truncated to exact boundary.")

async def async_bottleneck_test():
    logger.info("=== STARTING ASYNC BOTTLENECK TEST ===")
    
    pipeline = Pipeline()
    
    # Fire 3 simultaneous LangGraph requests
    logger.info("Firing 3 simultaneous Orchestrator requests...")
    
    start_time = time.time()
    
    # Pipeline.orchestrate makes a sleep of 1.0s and a network call
    # If they run asynchronously, 3 requests should take just over 1.0s total, not 3.0s+
    tasks = [pipeline.orchestrate(f"Request {i}", "model-a:latest") for i in range(3)]
    
    results = await asyncio.gather(*tasks)
    
    end_time = time.time()
    delta = end_time - start_time
    
    logger.info(f"All 3 requests resolved in {delta:.3f} seconds.")
    
    if delta < 1.5:
        logger.info("[SUCCESS] Open WebUI Uvicorn workers unblocked. Parallel resolution verified.")
    else:
        logger.error(f"[FAILED] Async bottleneck detected. Resolution took {delta:.3f} seconds (expected < 1.5s).")

async def run_all_tests():
    payload_truncation_test()
    print("\n")
    await async_bottleneck_test()
    print("\n")
    await vram_collision_injection()
    print("\n")
    logger.info("=== OMNI-SYSTEM STRESS TEST COMPLETE ===")

if __name__ == "__main__":
    asyncio.run(run_all_tests())
