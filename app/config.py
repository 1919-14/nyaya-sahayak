import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
CHROMA_DIR = BASE_DIR / "data" / "chroma"
RAW_DATA_DIR = BASE_DIR / "data" / "raw"

# ---------------------------------------------------------------------------
# LLM provider config — everything provider-specific lives in .env, NOT here.
# To switch providers (Groq, OpenAI, OpenRouter, Together, a local vLLM
# server, etc.) just edit LLM_API_KEY / LLM_BASE_URL / LLM_MODEL in .env.
# Any OpenAI-compatible /chat/completions endpoint works — Groq's API is
# OpenAI-compatible, which is why the default base URL below points at it.
# ---------------------------------------------------------------------------
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# Local, free, no API key needed. Swap for a bigger model if you have GPU time.
EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

# Chunking
CHUNK_SIZE_CHARS = 900
CHUNK_OVERLAP_CHARS = 150

# Below this similarity score, retrieval is considered "weak" and triggers
# the Tavily web fallback (if configured) or a clarifying question.
# Chroma returns cosine distance (lower = more similar) by default with this
# embedding model, so we convert to a 0-1 similarity score in retriever.py.
CONFIDENCE_THRESHOLD = 0.45

# Number of chunks to retrieve per query
TOP_K = 4

COLLECTION_NAME = "nyaya_sahayak_docs"

# Languages the assistant can respond in. Keys are what the frontend sends
# in ChatRequest.language; values are used inside LLM prompts.
SUPPORTED_LANGUAGES = {
    "en": "English",
    "hi": "Hindi (Devanagari script)",
}
DEFAULT_LANGUAGE = "en"
