"""
Offline ingestion: run this ONCE before the demo (not live), per the
architecture doc's "offline stage." It reads everything in data/raw/,
chunks it, and embeds it into the local Chroma DB.

Usage:
    python ingest/ingest.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import RAW_DATA_DIR, CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS
from app.vectorstore import add_chunks


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """
    Simple sliding-window character chunker. Splits on paragraph boundaries
    where possible so a chunk doesn't cut a clause in half mid-sentence.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks = []
    current = ""

    for para in paragraphs:
        if len(current) + len(para) + 2 <= size:
            current = f"{current}\n\n{para}" if current else para
        else:
            if current:
                chunks.append(current)
            # If a single paragraph is itself too long, hard-split it
            if len(para) > size:
                start = 0
                while start < len(para):
                    chunks.append(para[start:start + size])
                    start += size - overlap
                current = ""
            else:
                current = para

    if current:
        chunks.append(current)

    return chunks


def extract_domain(text: str) -> str:
    for line in text.splitlines()[:3]:
        if line.startswith("DOMAIN:"):
            return line.replace("DOMAIN:", "").strip()
    return "general"


def extract_source(text: str) -> str:
    for line in text.splitlines()[:3]:
        if line.startswith("SOURCE:"):
            return line.replace("SOURCE:", "").strip()
    return "unknown"


def main():
    files = sorted(RAW_DATA_DIR.glob("*.txt"))
    if not files:
        print(f"No .txt files found in {RAW_DATA_DIR}. Add source documents first.")
        return

    all_chunks = []
    for fpath in files:
        raw = fpath.read_text(encoding="utf-8")
        domain = extract_domain(raw)
        source = extract_source(raw)
        body = "\n\n".join(raw.split("\n\n")[1:]) if "DOMAIN:" in raw[:50] else raw

        pieces = chunk_text(body, CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS)
        print(f"{fpath.name}: domain='{domain}', {len(pieces)} chunks")

        for i, piece in enumerate(pieces):
            all_chunks.append({
                "id": f"{fpath.stem}_{i}",
                "text": piece,
                "domain": domain,
                "source": source,
            })

    print(f"\nEmbedding {len(all_chunks)} chunks into Chroma (first run downloads "
          f"the embedding model — needs internet)...")
    add_chunks(all_chunks)
    print("Done. Vector DB is ready at data/chroma/")


if __name__ == "__main__":
    main()
