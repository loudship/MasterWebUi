import re
import asyncio
import aiohttp
import logging
import json
from typing import List, Dict, Any

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger("Swarm_Orchestrator")

LM_STUDIO_API_URL = "http://host.docker.internal:1234/v1/chat/completions"

class ContextFirewall:
    def __init__(self, max_length: int = 20000):
        self.max_length = max_length
        self.b64_regex = re.compile(r'data:image\/[^;]+;base64,[a-zA-Z0-9+/]+={0,2}')
        
    def flatten_markdown_tables(self, text: str) -> str:
        """Naive conversion of markdown tables to pseudo-CSV to save tokens."""
        lines = text.split('\n')
        flattened = []
        for line in lines:
            if '|' in line:
                row = line.strip().strip('|')
                if re.match(r'^[\s\-|]+$', row):
                    continue
                cells = [cell.strip() for cell in row.split('|')]
                flattened.append(','.join(cells))
            else:
                flattened.append(line)
        return '\n'.join(flattened)

    def sanitize(self, payload: str) -> str:
        sanitized = self.b64_regex.sub('[IMAGE_REMOVED]', payload)
        sanitized = self.flatten_markdown_tables(sanitized)
        if len(sanitized) > self.max_length:
            sanitized = sanitized[:self.max_length] + "\n\n[WARNING: Payload truncated to preserve VRAM]"
        return sanitized

class Pipeline:
    """Open WebUI Pipeline for Swarm Orchestration"""
    def __init__(self):
        self.name = "Swarm Orchestrator"
        self.firewall = ContextFirewall(max_length=20000)
        self.qdrant_url = "http://host.docker.internal:6333/collections/test"

    async def call_llm(self, session: aiohttp.ClientSession, system_prompt: str, user_prompt: str, schema_name: str, schema_props: dict, model_id: str) -> str:
        """Helper method to construct prefix-optimized payload and strictly enforce structured output."""
        
        # Prefix Caching Geometry: Static instructions are locked at index 0
        api_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        # Enforce JSON Schema via structured outputs
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": schema_props,
                    "required": list(schema_props.keys()),
                    "additionalProperties": False
                }
            }
        }
        
        payload = {
            "model": model_id,
            "messages": api_messages,
            "response_format": response_format,
            "temperature": 0.1
        }
        
        logger.info(f"[ORCHESTRATOR] Prefix Cache aligned, requesting Structured JSON for schema '{schema_name}'")
        
        try:
            async with session.post(LM_STUDIO_API_URL, json=payload, timeout=60) as response:
                if response.status == 200:
                    data = await response.json()
                    content = data["choices"][0]["message"]["content"]
                    logger.info(f"[ORCHESTRATOR] Received Structured JSON for '{schema_name}'")
                    return content
                else:
                    error_text = await response.text()
                    logger.error(f"[ORCHESTRATOR] API Error: {response.status} - {error_text}")
                    return json.dumps({"error": f"LM Studio returned {response.status}"})
        except Exception as e:
            logger.error(f"[ORCHESTRATOR] Request failed: {e}")
            return json.dumps({"error": str(e)})

    async def run_lorekeeper(self, session: aiohttp.ClientSession, query: str, model_id: str) -> str:
        """Retrieves semantic vectors and uses an LLM to extract relevance."""
        try:
            async with session.get(self.qdrant_url, timeout=2) as response:
                await response.text() # simulate qdrant retrieval time
        except Exception:
            pass # Non-fatal for simulation
        
        system_prompt = (
            "You are the Lorekeeper node. Analyze the provided user query and simulate extracting "
            "relevant historical memory and context vectors from the local database. "
            "Maintain strict JSON format."
        )
        
        schema_props = {
            "relevant_topics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Topics relevant to the user query."
            },
            "extracted_context": {
                "type": "string",
                "description": "Simulated context retrieved from semantic vectors."
            }
        }
        
        return await self.call_llm(session, system_prompt, f"QUERY: {query}", "lorekeeper_output", schema_props, model_id)

    async def run_continuity_verifier(self, session: aiohttp.ClientSession, query: str, model_id: str) -> str:
        """Uses an LLM to analyze the logical flow and consistency of the request."""
        system_prompt = (
            "You are the Continuity Verifier node. Analyze the user request for logical consistency, "
            "potential contradictions, and alignment with safety boundaries. "
            "You must output only valid JSON."
        )
        
        schema_props = {
            "is_consistent": {
                "type": "boolean",
                "description": "Whether the user request is logically sound."
            },
            "analysis": {
                "type": "string",
                "description": "Detailed analysis of logical continuity."
            }
        }
        
        return await self.call_llm(session, system_prompt, f"QUERY: {query}", "continuity_output", schema_props, model_id)

    async def orchestrate(self, user_message: str, model_id: str) -> str:
        sanitized_input = self.firewall.sanitize(user_message)
        
        # Optimize concurrent connections
        connector = aiohttp.TCPConnector(limit=10, force_close=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Dispatch async agents concurrently
            lorekeeper_task = asyncio.create_task(self.run_lorekeeper(session, sanitized_input, model_id))
            continuity_task = asyncio.create_task(self.run_continuity_verifier(session, sanitized_input, model_id))
            
            lorekeeper_result, continuity_result = await asyncio.gather(lorekeeper_task, continuity_task)
            
            # Synthesizer Node
            system_prompt = (
                "You are the Synthesizer, the final output node of an Elite Autonomous Agentic Laboratory. "
                "Your instructions are to compile the findings from the Lorekeeper and Continuity Verifier "
                "into a single, coherent response to the user. Do not break character. "
                "Maintain strict logic and conciseness to preserve VRAM."
            )
            
            # Dynamic Context appended at the end of the array to maximize KV Cache Hit rate
            dynamic_context = (
                f"--- LOREKEEPER DATA ---\n{lorekeeper_result}\n\n"
                f"--- CONTINUITY DATA ---\n{continuity_result}\n\n"
                f"--- USER REQUEST ---\n{sanitized_input}"
            )
            
            schema_props = {
                "final_synthesis": {
                    "type": "string",
                    "description": "The complete, synthesized response to the user based strictly on the provided context."
                },
                "confidence_score": {
                    "type": "number",
                    "description": "A score from 0.0 to 1.0 representing response confidence."
                }
            }
            
            synthesis_json = await self.call_llm(session, system_prompt, dynamic_context, "synthesizer_output", schema_props, model_id)
            
            # Return raw JSON string to Open WebUI to demonstrate structured enforcement
            return synthesis_json

    async def pipe(self, user_message: str, model_id: str, messages: List[dict], body: dict) -> str:
        """Open WebUI Pipeline Entry Point. Supports native asyncio integration."""
        # Use body model parameter to explicitly map to the active LM Studio model
        target_model = body.get("model", model_id)
        result = await self.orchestrate(user_message, target_model)
        
        return result
