"""
Downloads real source documents from official Indian government sites for
each domain in the knowledge base, replacing the illustrative placeholder
text in data/raw/, then builds the vector DB from them in the same run.

This is the "real data" counterpart to ingest/ingest.py: run this instead of
(or before) ingest.py when you want the knowledge base grounded in actual
downloaded Act/scheme text rather than the hand-written summaries this
project shipped with.

Usage:
    python ingest/download_and_build.py             # download + build
    python ingest/download_and_build.py --no-build   # download only
    python ingest/download_and_build.py --build-only # build only (skip downloads,
                                                        use whatever is already
                                                        in data/raw/)

Requires internet access to the source domains below (mostly *.gov.in) —
this environment's sandbox could not reach them when this script was
written, so the download step is untested end-to-end from here; run it on
your own machine, where these should be reachable.
"""
from __future__ import annotations

import argparse
import io
import re
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import RAW_DATA_DIR, CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS
from app.vectorstore import add_chunks
from ingest.ingest import chunk_text, extract_domain, extract_source

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Source:
    filename: str        # data/raw/<filename>
    domain: str           # matches the DOMAIN: header ingest.py expects
    title: str            # matches the SOURCE: header ingest.py expects
    url: str
    kind: str             # "pdf" | "html"


# Real, official (mostly *.gov.in) sources — one entry per existing knowledge
# domain. Verify these still resolve before a real demo; government URLs do
# occasionally move. If a URL breaks, swap it for the current one from the
# same ministry/department's site rather than a third-party mirror.
SOURCES: list[Source] = [
    Source(
        "rti_act.txt", "Right to Information",
        "Right to Information Act, 2005 — official text",
        "https://cic.gov.in/sites/default/files/RTI-Act_English.pdf",
        "pdf",
    ),
    Source(
        "consumer_protection.txt", "Consumer Protection",
        "Consumer Protection Act, 2019 — official text (ncdrc.nic.in)",
        "https://ncdrc.nic.in/bare_acts/CPA2019.pdf",
        "pdf",
    ),
    Source(
        "labour_rights.txt", "Workplace and Labour Rights",
        "Code on Wages, 2019 — official text",
        "https://egazette.gov.in/WriteReadData/2019/210356.pdf",
        "pdf",
    ),
    Source(
        "welfare_scheme.txt", "Welfare Scheme Eligibility",
        "Ayushman Bharat PM-JAY — official overview",
        "https://pmjay.gov.in/sites/default/files/2018-09/PMJAY_Guidelines.pdf",
        "pdf",
    ),
]


class _VisibleTextExtractor(HTMLParser):
    """Minimal HTML-to-text extractor — stdlib only, no BeautifulSoup
    dependency. Drops script/style content and collapses whitespace."""

    _SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4"):
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.chunks.append(data)


def fetch(url: str) -> bytes:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, context=ctx, timeout=30) as response:
        return response.read()


def extract_pdf_text(raw_bytes: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw_bytes))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages)


def extract_html_text(raw_bytes: bytes) -> str:
    parser = _VisibleTextExtractor()
    parser.feed(raw_bytes.decode("utf-8", errors="ignore"))
    text = "".join(parser.chunks)
    # Collapse the excess whitespace/blank lines HTML text extraction leaves behind.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def clean_extracted_text(text: str) -> str:
    """Light cleanup common to government PDF extraction: repeated page
    headers/footers, stray form-feed characters, excessive blank lines.
    Deliberately conservative — this does not rewrite or summarize the
    source, only removes obvious extraction noise."""
    text = text.replace("\x0c", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def download_source(source: Source) -> bool:
    """Downloads one source, extracts its text, and writes it to
    data/raw/<filename> with the DOMAIN:/SOURCE: header ingest.py expects.
    Returns True on success, False on any failure (network, parsing) —
    failures are non-fatal so one broken URL doesn't stop the whole run."""
    print(f"Fetching {source.title} ...")
    try:
        raw_bytes = fetch(source.url)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"  FAILED to download: {exc}")
        return False

    try:
        text = extract_pdf_text(raw_bytes) if source.kind == "pdf" else extract_html_text(raw_bytes)
    except Exception as exc:  # extraction can fail in many library-specific ways
        print(f"  FAILED to extract text: {exc}")
        return False

    text = clean_extracted_text(text)
    if len(text) < 200:
        print(f"  WARNING: extracted text looks too short ({len(text)} chars) — "
              f"the source page may have changed structure. Skipping.")
        return False

    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DATA_DIR / source.filename
    header = f"DOMAIN: {source.domain}\nSOURCE: {source.title}\n\n"
    out_path.write_text(header + text, encoding="utf-8")
    print(f"  Saved {len(text):,} chars -> {out_path}")
    return True


def build_vector_db() -> None:
    files = sorted(RAW_DATA_DIR.glob("*.txt"))
    if not files:
        print(f"No .txt files found in {RAW_DATA_DIR} — nothing to build.")
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
                "id": f"{fpath.stem}_{i}", "text": piece, "domain": domain, "source": source,
            })

    print(f"\nEmbedding {len(all_chunks)} chunks into Chroma (first run downloads "
          f"the embedding model from Hugging Face — needs internet)...")
    add_chunks(all_chunks)
    print("Done. Vector DB is ready at data/chroma/")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-build", action="store_true",
                         help="Download sources only; don't build the vector DB.")
    parser.add_argument("--build-only", action="store_true",
                         help="Skip downloads; build the vector DB from whatever "
                              "is already in data/raw/.")
    args = parser.parse_args()

    if args.build_only and args.no_build:
        parser.error("--build-only and --no-build together do nothing — pick one.")

    if not args.build_only:
        print(f"Downloading {len(SOURCES)} real source document(s)...\n")
        results = [download_source(s) for s in SOURCES]
        succeeded = sum(results)
        print(f"\n{succeeded}/{len(SOURCES)} sources downloaded successfully.")
        if succeeded < len(SOURCES):
            print("Sources that failed keep whatever was already in data/raw/ "
                  "for that file (the placeholder text, if this is a fresh clone) "
                  "— check the errors above and retry, or fetch that one manually.")

    if not args.no_build:
        print()
        build_vector_db()


if __name__ == "__main__":
    main()
