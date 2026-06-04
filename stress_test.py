import asyncio
from langchain_core.documents import Document
from open_webui.retrieval.vector.main import VectorItem
from open_webui.config import RAG_EMBEDDING_ENGINE, RAG_EMBEDDING_MODEL

# Synthetic heavy content
text = "The quick brown fox jumps over the lazy dog. " * 50
text += "Contact alice.smith@google.com, bob.jones@microsoft.com, charlie.brown@amazon.com, david.lee@apple.com. "
text += "Meeting with John Doe, Jane Smith, Robert Johnson, Emily Davis in New York, London, Tokyo, Paris. "
text += "System architecture by Linus Torvalds, Bill Gates, Steve Jobs. " * 10

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
        from open_webui.routers.retrieval import get_ef
        self.ef = get_ef(self.config.RAG_EMBEDDING_ENGINE, self.config.RAG_EMBEDDING_MODEL)
        self.main_loop = asyncio.get_event_loop()

class MockApp:
    def __init__(self):
        self.state = MockState()

class MockRequest:
    def __init__(self):
        self.app = MockApp()

import time
import threading

def worker_thread(request, docs):
    try:
        from open_webui.routers.retrieval import save_docs_to_vector_db
        class DummyUser:
            id = "test_user"
        save_docs_to_vector_db(request, docs, "test_collection", {"file_id": "test_id", "hash": "test_hash"}, split=True, user=DummyUser())
    except Exception as e:
        print(f"Error in worker: {e}")

async def run_test():
    try:
        request = MockRequest()
        docs = [Document(page_content=text, metadata={"source": "stress_test.md"})]
        
        start_time = time.time()
        print("Starting save_docs_to_vector_db...")
        thread = threading.Thread(target=worker_thread, args=(request, docs))
        thread.start()
        
        # Keep asyncio loop running so run_coroutine_threadsafe can execute
        while thread.is_alive():
            await asyncio.sleep(0.1)
            
        end_time = time.time()
        print(f"Execution time: {end_time - start_time:.4f} seconds")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(run_test())
