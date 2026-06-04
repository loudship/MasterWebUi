import re
from typing import List, Optional, Dict, Any
from pydantic import BaseModel

class Pipeline:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.name = "NER Document Semantic Filter Pipeline"
        self.valves = self.Valves()

    async def on_startup(self):
        print(f"on_startup: {self.name} initialized.")

    async def on_shutdown(self):
        print(f"on_shutdown: {self.name} terminating.")

    def extract_entities_cpu(self, text: str) -> list:
        """Zero-shot NER using pure Python regex to protect GPU VRAM."""
        entities = []
        # Extract emails
        emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
        entities.extend(emails)
        
        # Extract Proper Nouns (capitalized word sequences)
        propn = re.findall(r'\b[A-Z][a-z]+(?: [A-Z][a-z]+)*\b', text)
        for p in propn:
            if len(p.split()) > 1 and p not in entities:
                entities.append(p)
                
        return list(set(entities))

    def inlet(self, body: dict, user: Optional[dict] = None) -> dict:
        """
        Standard Open WebUI pipeline inlet. Intercepts chat requests.
        """
        print(f"inlet: received chat body for RAG intercept.")
        return body

    def filter_document(self, document: Dict[str, Any]) -> Dict[str, Any]:
        """
        Intercepts RAG document ingestion chunking event.
        Extracts entities on the CPU and appends to the chunk metadata.
        """
        if "text" in document:
            entities = self.extract_entities_cpu(document["text"])
            if "metadata" not in document:
                document["metadata"] = {}
            document["metadata"]["entities"] = entities
            print(f"Extracted {len(entities)} entities for vector chunk.")
        elif "page_content" in document: # Langchain Document object compatibility
            entities = self.extract_entities_cpu(document["page_content"])
            if "metadata" not in document:
                document["metadata"] = {}
            document["metadata"]["entities"] = entities
            print(f"Extracted {len(entities)} entities for langchain chunk.")
            
        return document

    # Document Hook (Open WebUI specific document processing override if supported)
    def on_document_ingest(self, document, user=None):
        return self.filter_document(document)

