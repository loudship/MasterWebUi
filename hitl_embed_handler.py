"""
name: HITL HTML Embed Handler
description: Pushes an HTML iframe to the WebUI chat for Human-in-the-Loop decision capture and blocks execution via Redis.
author: Admin
version: 1.0
"""
from pydantic import BaseModel, Field
import redis.asyncio as redis
from typing import Optional
import json
import uuid

class Action:
    class Valves(BaseModel):
        REDIS_BROKER_URL: str = Field(default="redis://redis-cache:6379/0", description="Cache broker connection string.")
        WEBSOCKET_EVENT_CALLER_TIMEOUT: int = Field(default=300, description="Execution wait window in seconds.")
        DECISION_VALVE_MODE: str = Field(default="APPROVE", description="Action class selector parameter.")

    def __init__(self):
        self.valves = self.Valves()

    async def action(self, body: dict, __user__=None, __event_emitter__=None, __valves__=None) -> Optional[dict]:
        messages = body.get("messages", [])
        if not messages:
            return

        last_message = messages[-1]
        metadata = last_message.get("metadata", {})
        request_id = metadata.get("hitl_request_id", str(uuid.uuid4()))

        # 1. Status Event Route
        if __event_emitter__:
            await __event_emitter__({
                "type": "status",
                "data": {
                    "description": "Orchestrator Paused. Pending Manual Verification...",
                    "done": False
                }
            })

        # 2. HTML Rich UI Embed Construction Step
        html_payload = f"""
        <html>
        <head>
            <style>
                body {{ font-family: 'Inter', sans-serif; padding: 24px; background: rgba(20,20,20,0.8); color: #fff; text-align: center; border-radius: 12px; margin: 0; backdrop-filter: blur(10px); }}
                h3 {{ margin-top: 0; color: #ffcc00; }}
                .btn {{ padding: 12px 24px; margin: 10px; border: none; border-radius: 6px; cursor: pointer; font-weight: bold; transition: 0.2s; }}
                .btn-approve {{ background: #28a745; color: white; }}
                .btn-approve:hover {{ background: #218838; }}
                .btn-reject {{ background: #dc3545; color: white; }}
                .btn-reject:hover {{ background: #c82333; }}
            </style>
        </head>
        <body>
            <h3>⚠️ Human Intervention Required</h3>
            <p>Target graph execution suspended. Please approve the workflow constraint.</p>
            <button class="btn btn-approve" onclick="window.parent.postMessage({{type: 'action', action: 'APPROVE', id: '{request_id}'}}, '*')">✅ Approve Execution</button>
            <button class="btn btn-reject" onclick="window.parent.postMessage({{type: 'action', action: 'REJECT', id: '{request_id}'}}, '*')">❌ Abort Mission</button>
        </body>
        </html>
        """

        # Push sandboxed HTML using `replace=True` to prevent layout shift accumulation
        if __event_emitter__:
            await __event_emitter__({
                "type": "embeds",
                "data": {
                    "embeds": [html_payload],
                    "replace": True
                }
            })

        # 3. Valkey/Redis Wait State Registration Step
        try:
            r = redis.from_url(self.valves.REDIS_BROKER_URL, decode_responses=True)
            queue_key = f"hitl:ui_capture:{request_id}"
            
            # Instantly halt backend execution via BLPOP
            result = await r.blpop(queue_key, timeout=self.valves.WEBSOCKET_EVENT_CALLER_TIMEOUT)
            
            if result:
                _, decision = result
                
                # Unblock orchestrator BLPOP
                await r.lpush(f"hitl:response:{request_id}", decision)
                
                if __event_emitter__:
                    await __event_emitter__({
                        "type": "status",
                        "data": {
                            "description": f"Operator selected: {decision}. Resuming graph...",
                            "done": True
                        }
                    })
            else:
                # 4. Timeout Exception
                if __event_emitter__:
                    await __event_emitter__({
                        "type": "status",
                        "data": {
                            "description": "HITL Request Timeout (300s). Execution Aborted.",
                            "done": True
                        }
                    })
                # Auto-reject on timeout
                await r.lpush(f"hitl:response:{request_id}", "REJECT")
                
            await r.aclose()
        except Exception as e:
            if __event_emitter__:
                await __event_emitter__({
                    "type": "status",
                    "data": {
                        "description": f"Critical Error in HITL Wait State: {e}",
                        "done": True
                    }
                })
