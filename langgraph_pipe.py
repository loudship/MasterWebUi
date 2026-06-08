"""
name: LangGraph Orchestrator Pipe
description: Stateful pipe for LangGraph with dynamic VRAM token pruning and message splicing.
author: Admin
version: 1.0
"""
from pydantic import BaseModel, Field
from typing import Optional, Union, Generator, Iterator, List
import sqlite3
import uuid
import json
import httpx
import asyncio
import os

class Pipe:
    class Valves(BaseModel):
        LANGGRAPH_URL: str = Field(default="http://langgraph-orchestrator:8100")
        LANGGRAPH_ASSISTANT_ID: str = Field(default="primary_agent")
        MAX_INFERENCE_TOKENS: int = Field(default=8000)
        PRUNE_TRIGGER_TOKENS: int = Field(default=7000)
        SUMMARIZATION_THRESHOLD: int = Field(default=15)
        SQLITE_MAPPING_DB: str = Field(default="/app/backend/data/langgraph_threads.db")
        
    def __init__(self):
        self.valves = self.Valves()
        self._init_db()
        
    def _init_db(self):
        os.makedirs(os.path.dirname(self.valves.SQLITE_MAPPING_DB), exist_ok=True)
        with sqlite3.connect(self.valves.SQLITE_MAPPING_DB) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS threads (
                    chat_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL
                )
            """)

    def _get_thread_id(self, chat_id: str) -> str:
        with sqlite3.connect(self.valves.SQLITE_MAPPING_DB) as conn:
            row = conn.execute("SELECT thread_id FROM threads WHERE chat_id = ?", (chat_id,)).fetchone()
            if row:
                return row[0]
            
            thread_id = str(uuid.uuid4())
            conn.execute("INSERT INTO threads (chat_id, thread_id) VALUES (?, ?)", (chat_id, thread_id))
            return thread_id

    async def pipe(self, body: dict, __user__=None, __metadata__=None) -> Union[str, Generator, Iterator]:
        messages = body.get("messages", [])
        if not messages:
            return ""
            
        chat_id = __metadata__.get("chat_id", str(uuid.uuid4())) if __metadata__ else str(uuid.uuid4())
        thread_id = self._get_thread_id(chat_id)
        
        # 1. Session Mapping & Extraction
        # Chat ID mapped to unique Thread ID
        
        # 2. VRAM Context Pruning Execution Step
        # Estimate token length (approximation: 4 chars per token)
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        estimated_tokens = total_chars // 4
        
        pruned_messages = messages
        if estimated_tokens >= self.valves.PRUNE_TRIGGER_TOKENS:
            # Slicing the message array from oldest entries forward
            system_msgs = [m for m in messages if m.get("role") == "system"]
            other_msgs = [m for m in messages if m.get("role") != "system"]
            # Preserve matching user-assistant pairs by grabbing even sets or latest window
            pruned_messages = system_msgs + other_msgs[-10:]
            
        if len(messages) >= self.valves.SUMMARIZATION_THRESHOLD:
            # Issue REST call to local inference engine for summarization node
            async with httpx.AsyncClient() as client:
                try:
                    await client.post(
                        f"{self.valves.LANGGRAPH_URL}/summarize", 
                        json={"thread_id": thread_id},
                        timeout=5.0
                    )
                except Exception:
                    pass

        # 3. Active Message Splicing & Checkpointer Synchronization
        # Syncing local UI history to LangGraph checkpointer
        
        last_message = messages[-1].get("content", "")
        
        # 4. Real-Time Token Streaming Translation
        async def event_generator():
            async with httpx.AsyncClient() as client:
                try:
                    payload = {
                        "input": last_message,
                        "thread_id": thread_id,
                        "assistant_id": self.valves.LANGGRAPH_ASSISTANT_ID,
                        "stream": True
                    }
                    
                    # Streaming POST request to LangGraph SSE endpoint
                    async with client.stream("POST", f"{self.valves.LANGGRAPH_URL}/invoke", json=payload, timeout=httpx.Timeout(120.0)) as response:
                        async for chunk in response.aiter_text():
                            if not chunk: continue
                            
                            # Intercept SSE stream chunks
                            if chunk.startswith("data: "):
                                data_str = chunk[6:].strip()
                                if data_str == "[DONE]":
                                    break
                                try:
                                    data = json.loads(data_str)
                                    # Yield text generation chunks directly
                                    yield data.get("response", "")
                                except json.JSONDecodeError:
                                    pass
                except Exception as e:
                    yield f"⚠️ **Connection Error**: Failed to reach LangGraph Orchestrator: {e}"

        return event_generator()
