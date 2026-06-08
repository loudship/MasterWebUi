import asyncio
import re
import aiohttp
from typing import Optional
from pydantic import BaseModel

class Filter:
    class Valves(BaseModel):
        LANGGRAPH_URL: str = "http://langgraph-orchestrator:8100/invoke"

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        messages = body.get("messages", [])
        if not messages:
            return body

        last_message = messages[-1]
        if last_message.get("role") != "user":
            return body

        content = last_message.get("content", "")
        
        MAX_CHARS = 20000
        if len(content) > MAX_CHARS:
            content = content[:MAX_CHARS]
            content += "\n\n> [!WARNING]\n> **Context Restriction Enforcement:** Your message exceeded the 20,000 character limit and was truncated by the pipeline filter to protect the inference buffer."
        
        entities = self.extract_entities(content)
        metadata_block = f"\n[METADATA: Entities -> {', '.join(entities)}]" if entities else "\n[METADATA: Entities -> None]"
        content += metadata_block
        
        last_message["content"] = content
        body["messages"][-1] = last_message

        async def forward_payload(payload_body):
            try:
                async with aiohttp.ClientSession() as session:
                    payload = {"input": payload_body["messages"][-1]["content"]}
                    async with session.post(self.valves.LANGGRAPH_URL, json=payload, timeout=60) as resp:
                        await resp.text()
            except Exception as e:
                print(f"Filter Async Forward Error: {e}")

        # Ensure this executes without blocking the main async event loop
        asyncio.create_task(forward_payload(body))

        return body

    async def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        return body

    def extract_entities(self, text: str) -> list[str]:
        # Native CPU-driven string-matching matrix
        matches = set(re.findall(r'\b[A-Z][a-z]+\b', text))
        stopwords = {"The", "A", "An", "In", "On", "At", "To", "Is", "Are", "And", "Or", "If", "It", "This"}
        return [m for m in matches if m not in stopwords]
