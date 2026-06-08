import json
from typing import Dict, Any, Generator

def emit_hitl_request(tool_name: str, tool_arguments: Dict[str, Any], request_id: str) -> Generator[Dict[str, Any], None, None]:
    """
    Integrated into the LangGraph orchestrator.
    Yields the required SSE payloads to Open WebUI to render the HITL prompt
    and securely pass the request_id via message metadata.
    """
    # 1. Yield the visual prompt for the operator
    yield {
        "type": "message",
        "content": (
            f"\n\n🚨 **High-Risk Tool Execution Paused** 🚨\n\n"
            f"**Target Tool:** `{tool_name}`\n\n"
            f"**Payload:**\n```json\n{json.dumps(tool_arguments, indent=2)}\n```\n\n"
            f"> Please utilize the **HITL Action Buttons** below this message to proceed.\n"
        )
    }
    
    # 2. Yield the metadata payload containing the secure request_id
    # The Open WebUI Action Function will extract this metadata from the message
    yield {
        "type": "metadata",
        "data": {
            "hitl_request_id": request_id,
            "hitl_tool_name": tool_name
        }
    }
