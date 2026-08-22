"""
Thin wrapper around a local Chroma collection.

Used both by the offline ingest script (to write) and the live retriever
(to read). Keeping this in one place means the embedding model is loaded
consistently for indexing and querying.
"""
from __future__ import annotations

import chromadb
from chromadb.utils import embedding_functions

from app.config import CHROMA_DIR, COLLECTION_NAME, EMBEDDING_MODEL_NAME


def get_collection():
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))

    embedder = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBEDDING_MODEL_NAME
    )

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=embedder,
        metadata={"hnsw:space": "cosine"},
    )
    return collection


def add_chunks(chunks: list[dict]) -> None:
    """
    chunks: list of {"id": str, "text": str, "domain": str, "source": str}
    """
    if not chunks:
        return
    collection = get_collection()
    collection.upsert(
        ids=[c["id"] for c in chunks],
        documents=[c["text"] for c in chunks],
        metadatas=[{"domain": c["domain"], "source": c["source"]} for c in chunks],
    )


def query(text: str, top_k: int = 4) -> dict:
    """
    Returns raw Chroma query result: documents, metadatas, distances (all
    lists-of-lists, one inner list per query — we only ever pass one query).
    """
    collection = get_collection()
    return collection.query(query_texts=[text], n_results=top_k)
