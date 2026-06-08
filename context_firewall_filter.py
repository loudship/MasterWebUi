"""
name: Context Firewall Filter
description: Prunes unauthorized massive Base64 blobs from chat messages to prevent VRAM OOM errors, while allowing authorized layout metadata.
author: Admin
version: 1.0
"""
from pydantic import BaseModel, Field
import os
import re

class Filter:
    class Valves(BaseModel):
        MAX_CHAR_LIMIT: int = Field(default=20000, description="Maximum characters for base64 strings.")
        ENABLE_OPENAI_IMAGE_URL: bool = Field(default=True, description="Allow Layout-Aware OCR Image Injection.")
        
    def __init__(self):
        self.valves = self.Valves()
        # Override with env var if present
        env_enable = os.getenv("ENABLE_OPENAI_IMAGE_URL", "True").lower() == "true"
        self.valves.ENABLE_OPENAI_IMAGE_URL = env_enable

    def inlet(self, body: dict, __user__=None) -> dict:
        messages = body.get("messages", [])
        
        # Regex to detect base64 images
        b64_pattern = re.compile(r'(data:image/[^;]+;base64,)([A-Za-z0-9+/=]+)')
        
        for msg in messages:
            content = msg.get("content", "")
            
            if isinstance(content, list):
                # Process structured multi-modal blocks
                for block in content:
                    if block.get("type") == "image_url":
                        url = block.get("image_url", {}).get("url", "")
                        if url.startswith("data:image"):
                            if len(url) > self.valves.MAX_CHAR_LIMIT and not self.valves.ENABLE_OPENAI_IMAGE_URL:
                                block["image_url"]["url"] = "data:image/png;base64,BLOCKED"
                                
            elif isinstance(content, str):
                # Process raw markdown string injections
                matches = b64_pattern.findall(content)
                for prefix, b64_data in matches:
                    if len(b64_data) > self.valves.MAX_CHAR_LIMIT and not self.valves.ENABLE_OPENAI_IMAGE_URL:
                        # Strip massive base64 blob
                        target_string = prefix + b64_data
                        replacement = prefix + "BLOCKED_DUE_TO_VRAM_SAFETY_LIMITS"
                        content = content.replace(target_string, replacement)
                
                msg["content"] = content
                
        body["messages"] = messages
        return body
