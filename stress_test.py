import asyncio
import re
import logging
from typing import List, Set

logger = logging.getLogger(__name__)

# Comprehensive list of common non-entity words to filter
STOPWORDS = {
    "The", "A", "An", "In", "On", "At", "To", "For", "Of", "And", "Or", "But",
    "Is", "Are", "Was", "Were", "Be", "Been", "Being",
    "This", "That", "These", "Those", "It", "Its",
    "If", "Then", "Else", "While", "Do", "Does",
    "With", "From", "By", "As", "Per",
    "System", "New", "User", "File", "Data", "Content", "Page",
    "Meeting", "Discussion", "Document", "Report", "Section",
    "Example", "Note", "Comment", "Result", "Summary",
}

def extract_entities_cpu(text: str) -> List[dict]:
    """
    Lightweight regex-based NER extraction.
    Extracts emails, dates, and proper nouns with improved filtering.
    
    Args:
        text: Text to extract entities from
        
    Returns:
        List of entity dicts with 'type' and 'value' keys
    """
    entities = []
    seen = set()  # Prevent duplicates
    
    # Extract emails (high confidence)
    emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
    for email in emails:
        if email.lower() not in seen:
            entities.append({"type": "EMAIL", "value": email})
            seen.add(email.lower())
    
    # Extract dates (YYYY-MM-DD, MM/DD/YYYY, etc.)
    dates = re.findall(
        r'(?:\d{4}[-/]\d{2}[-/]\d{2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
        text
    )
    for date in dates:
        if date not in seen:
            entities.append({"type": "DATE", "value": date})
            seen.add(date)
    
    # Extract proper nouns (capitalized words, but exclude common stops and sentence starts)
    # Use negative lookbehind to avoid sentence starts
    propn = re.findall(r'(?<![.!?]\s)(?<![.!?\n]\s)\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b', text)
    
    for term in propn:
        # Skip stopwords and short words
        if term not in STOPWORDS and len(term) > 2 and term.lower() not in seen:
            entities.append({"type": "PROPN", "value": term})
            seen.add(term.lower())
    
    # Extract phone numbers (US format and variations)
    phones = re.findall(
        r'(?:\+1[-.]?)?\(?(?:\d{3})\)?[-.\s]?(?:\d{3})[-.\s]?(?:\d{4})\b',
        text
    )
    for phone in phones:
        if phone not in seen:
            entities.append({"type": "PHONE", "value": phone})
            seen.add(phone)
    
    return entities


class MockConfig:
    RAG_EMBEDDING_ENGINE = ""
    RAG_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
    ENABLE_MARKDOWN_HEADER_TEXT_SPLITTER = False
    TEXT_SPLITTER = 'character'
    CHUNK_SIZE = 1500
    CHUNK_OVERLAP = 100
    RAG_OPENAI_API_BASE_URL = ""
    RAG_OPENAI_API_KEY = ""
    RAG_OLLAMA_BASE_URL = ""
    RAG_OLLAMA_API_KEY = ""
    RAG_AZURE_OPENAI_BASE_URL = ""
    RAG_AZURE_OPENAI_API_KEY = ""
    RAG_AZURE_OPENAI_API_VERSION = ""
    RAG_EMBEDDING_BATCH_SIZE = 1
    ENABLE_ASYNC_EMBEDDING = False
    RAG_EMBEDDING_CONCURRENT_REQUESTS = 0
    CHUNK_MIN_SIZE_TARGET = 0


class MockState:
    def __init__(self):
        self.config = MockConfig()
        # Note: In production, this would be initialized from request context
        self.main_loop = None


class MockApp:
    def __init__(self):
        self.state = MockState()


class MockRequest:
    def __init__(self):
        self.app = MockApp()


async def run_test():
    """Run entity extraction test on synthetic data."""
    try:
        # Synthetic heavy content
        text = "The quick brown fox jumps over the lazy dog. " * 5
        text += "Contact alice.smith@google.com, bob.jones@microsoft.com for more details. "
        text += "Meeting scheduled for 2024-12-25 with John Doe, Jane Smith, and Robert Johnson in New York. "
        text += "Phone: +1-555-123-4567 or (555) 987-6543. "
        text += "System architecture designed by Linus Torvalds and Bill Gates. "
        
        logger.info("Starting entity extraction test...")
        logger.info(f"Text sample: {text[:200]}...")
        
        entities = extract_entities_cpu(text)
        
        logger.info(f"Extracted {len(entities)} entities:")
        for entity in entities:
            logger.info(f"  {entity['type']}: {entity['value']}")
        
        # Verify extraction
        assert any(e['type'] == 'EMAIL' for e in entities), "No emails extracted"
        assert any(e['type'] == 'PROPN' for e in entities), "No proper nouns extracted"
        
        logger.info("Entity extraction test completed successfully")
        return True
        
    except Exception as e:
        logger.error(f"Test error: {e}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    success = asyncio.run(run_test())
    exit(0 if success else 1)
