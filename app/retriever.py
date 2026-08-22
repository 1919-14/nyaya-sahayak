"""
Retrieval + confidence gating + web fallback.

Flow (matches the architecture doc):
  1. Search the local vector DB first.
  2. If the best match is confident, use it (source_tier = "verified").
  3. If not, and TAVILY_API_KEY is set, fall back to a live web search
     (source_tier = "web-sourced").
  4. If neither yields a confident result, the caller (main.py) should ask
     a clarifying question instead of answering.
"""
from dataclasses import dataclass, field

from app.config import TAVILY_API_KEY, CONFIDENCE_THRESHOLD, TOP_K
from app.vectorstore import query as vector_query

try:
    from tavily import TavilyClient
    _tavily = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None
except ImportError:
    _tavily = None


@dataclass
class RetrievedChunk:
    text: str
    source: str
    domain: str
    score: float          # 0-1, higher = more confident
    source_tier: str      # "verified" | "web-sourced"
    url: str | None = None


@dataclass
class RetrievalResult:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    confident: bool = False
    used_fallback: bool = False


def _distance_to_similarity(distance: float) -> float:
    # Chroma cosine distance is in [0, 2]; convert to a 0-1 similarity score
    # where 1.0 = identical. Clamped defensively.
    similarity = 1.0 - (distance / 2.0)
    return max(0.0, min(1.0, similarity))


def retrieve_local(query_text: str, top_k: int = TOP_K) -> list[RetrievedChunk]:
    raw = vector_query(query_text, top_k=top_k)
    if not raw["documents"] or not raw["documents"][0]:
        return []

    chunks = []
    for doc, meta, dist in zip(
        raw["documents"][0], raw["metadatas"][0], raw["distances"][0]
    ):
        chunks.append(RetrievedChunk(
            text=doc,
            source=meta.get("source", "unknown"),
            domain=meta.get("domain", "general"),
            score=_distance_to_similarity(dist),
            source_tier="verified",
        ))
    return sorted(chunks, key=lambda c: c.score, reverse=True)


def retrieve_web(query_text: str, max_results: int = 3) -> list[RetrievedChunk]:
    if not _tavily:
        return []

    result = _tavily.search(
        query=f"{query_text} India law government",
        search_depth="advanced",
        max_results=max_results,
    )
    chunks = []
    for item in result.get("results", []):
        chunks.append(RetrievedChunk(
            text=item.get("content", ""),
            source=item.get("title", "web result"),
            domain="web",
            score=item.get("score", 0.5),
            source_tier="web-sourced",
            url=item.get("url"),
        ))
    return chunks


def retrieve(query_text: str, web_search: bool = False, status_callback=None) -> RetrievalResult:
    if status_callback:
        status_callback("searching_local")

    local_chunks = retrieve_local(query_text)
    best_local_score = local_chunks[0].score if local_chunks else 0.0

    # If web_search is explicitly requested by user toggle
    if web_search:
        if status_callback:
            status_callback("searching_web")
        web_chunks = retrieve_web(query_text)
        combined = local_chunks + web_chunks
        return RetrievalResult(
            chunks=combined if combined else local_chunks,
            confident=True,
            used_fallback=True
        )

    if best_local_score >= CONFIDENCE_THRESHOLD:
        return RetrievalResult(chunks=local_chunks, confident=True, used_fallback=False)

    # Local retrieval was weak — try the web fallback.
    if status_callback:
        status_callback("searching_web")

    web_chunks = retrieve_web(query_text)
    if web_chunks:
        combined = local_chunks + web_chunks
        return RetrievalResult(chunks=combined, confident=True, used_fallback=True)

    # Nothing confident found anywhere — caller should trigger a clarifying question.
    return RetrievalResult(chunks=local_chunks, confident=False, used_fallback=False)
