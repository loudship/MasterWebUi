"""
name: HITL Decision Action
description: Submits an APPROVE or REJECT binary decision to the Redis cache to resume graph execution.
author: Admin
version: 1.0
"""

import json
from typing import Optional
from pydantic import BaseModel, Field
import redis.asyncio as redis

class Action:
    class Valves(BaseModel):
        redis_url: str = Field(
            default="redis://redis-cache:6379/0",
            description="The connection string to the Redis cache."
        )
        decision: str = Field(
            default="APPROVE",
            description="The binary decision to push to the queue (APPROVE or REJECT). Configure two separate actions in Open WebUI to display both buttons."
        )

    def __init__(self):
        self.valves = self.Valves()

    async def action(
        self,
        body: dict,
        __user__=None,
        __event_emitter__=None,
        __valves__=None,
    ) -> Optional[dict]:
        
        # 1. Provide immediate feedback to the operator that the UI is processing the click
        if __event_emitter__:
            await __event_emitter__({
                "type": "status",
                "data": {
                    "description": f"Transmitting {self.valves.decision} signal to orchestrator...",
                    "done": False
                }
            })

        # 2. Extract the request_id from the metadata yielded by hitl_event_emitter.py
        messages = body.get("messages", [])
        if not messages:
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Error: No message context found.", "done": True}})
            return

        last_message = messages[-1]
        metadata = last_message.get("metadata", {})
        request_id = metadata.get("hitl_request_id")
        tool_name = metadata.get("hitl_tool_name", "unknown_tool")

        if not request_id:
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Error: Missing hitl_request_id in message metadata.", "done": True}})
            return

        # 3. Establish async connection to Redis and execute LPUSH
        try:
            r = redis.from_url(self.valves.redis_url, decode_responses=True)
            response_queue = f"hitl:response:{request_id}"
            
            # Pushing the decision to collapse the orchestrator's BLPOP wait-state
            await r.lpush(response_queue, self.valves.decision)
            await r.aclose()
            
            # 4. Yield success feedback back to the UI
            success_msg = "✅ Authorization granted." if self.valves.decision == "APPROVE" else "❌ Execution aborted."
            
            if __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": f"{success_msg} Resuming orchestrator for {tool_name}...",
                        "done": True
                    }
                })
                
                # Optionally append the system notification to the chat text itself
                await __event_emitter__({
                    "type": "message",
                    "data": {"content": f"\n\n*[SYSTEM] {success_msg} Operator decision logged for {tool_name}.*"}
                })
                
        except Exception as e:
            if __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": f"Critical Error pushing to Redis: {str(e)}",
                        "done": True
                    }
                })
