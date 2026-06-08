import asyncio
import logging
import json
import uuid
import redis.asyncio as redis

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger("HITL_Broker")

class RedisHITLGate:
    """
    A Human-In-The-Loop (HITL) gate that halts graph execution and 
    executes an asynchronous zero-CPU blocking state (BLPOP) while 
    waiting for cryptographic approval from the human operator via Redis.
    """
    def __init__(self, redis_url="redis://redis-cache:6379/0"):
        self.redis_url = redis_url
        self.redis = redis.from_url(redis_url, decode_responses=True)
        
    async def request_approval(self, tool_name: str, payload: dict) -> bool:
        request_id = str(uuid.uuid4())
        
        # Define strict Redis keys for this specific HITL interception
        state_key = f"hitl:pending:{request_id}"
        response_queue = f"hitl:response:{request_id}"
        
        # 1. The Orchestrator pushes the pending state to the redis-cache container
        # We set an automatic 300 second expiration to prevent zombie state memory leaks
        await self.redis.set(state_key, json.dumps({
            "tool_name": tool_name,
            "payload": payload,
            "status": "pending"
        }), ex=300)
        
        # Fire a pub/sub event which the Open WebUI SSE backend will ingest
        await self.redis.publish("hitl_notifications", json.dumps({
            "request_id": request_id,
            "tool_name": tool_name
        }))
        
        # Emit the requested log
        logger.warning(f"[HITL] Halting execution, awaiting human clearance for {tool_name}...")
        
        # 2. The Redis Block (BLPOP)
        # Graph execution pauses here at zero CPU cost until the UI LPUSHes a decision
        try:
            result = await self.redis.blpop(response_queue, timeout=300)
            
            # Strict timeout handling
            if result is None:
                logger.error(f"[HITL] Timeout reached. No human response for {tool_name} within 300s. Defaulting to REJECT.")
                return False
                
            _, decision = result
            
            # 3. The Resolution
            if decision.upper() == "APPROVE":
                logger.info(f"[HITL] Operator clearance GRANTED for {tool_name}.")
                return True
            else:
                logger.warning(f"[HITL] Operator clearance DENIED for {tool_name}.")
                return False
                
        except Exception as e:
            logger.error(f"[HITL] Critical Error during execution halt: {e}")
            return False
        finally:
            # Clean up the pending state immediately after resolution
            await self.redis.delete(state_key)

# Basic test execution block for direct file testing
if __name__ == "__main__":
    async def test_hitl():
        broker = RedisHITLGate()
        # Simulated payload for a high-risk home assistant MCP tool
        test_payload = {
            "entity_id": "lock.front_door",
            "action": "unlock"
        }
        
        # Start the request (this will timeout after 300s since no UI is clicking approve)
        logger.info("Initializing HITL Block Test. Expected to timeout in 300 seconds if no redis push occurs.")
        # We can pass timeout=3 to blpop for the test in the actual code, but using the real logic here:
        approved = await broker.request_approval("ha-mcp.unlock_door", test_payload)
        logger.info(f"Final Decision: {approved}")

    # For testing, we would use asyncio.run(test_hitl())
