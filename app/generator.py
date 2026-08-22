"""
Turns retrieved chunks (+ optional uploaded-document context) into either:
  - a grounded chat answer with inline citations and source-tier labels, or
  - a clarifying question, when confidence is too low / sources conflict.

Also provides:
  - streaming variants (generate_answer_stream / simplify_answer_stream) used
    by the SSE endpoints in main.py so the UI can render tokens as they arrive.
  - simplify_answer_stream(): the "Explain simpler" feature. Reuses the
    already-retrieved chunks from the last turn — no new retrieval — per the
    architecture doc.

Clarifying questions are structured (question + short selectable options),
not just free text — the frontend renders the options as tappable buttons
plus a "type your own" fallback, so the user isn't stuck typing everything.

Every prompt below accepts a `language` code ("en" | "hi") and asks the model
to respond in that language — this is the actual language toggle, not a
cosmetic UI-only switch.
"""
import json
import re
from typing import Iterator

from app import llm_client
from app.config import SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE
from app.retriever import RetrievalResult

ANSWER_SYSTEM_PROMPT = """You are a civic/legal-rights assistant for Indian citizens.
You explain rights and procedures in plain language, grounded ONLY in the provided
source chunks. You are not a lawyer and you never invent legal facts.

Rules:
- Every factual claim must be traceable to one of the provided sources. Reference
  sources inline like [1], [2] matching the numbered source list you're given.
- If sources marked "web-sourced" conflict with "verified" sources, trust "verified"
  and note the discrepancy briefly.
- If the provided sources are insufficient to fully answer, say what you CAN answer,
  then clearly state what remains uncertain — do not fill the gap with a guess.
- Keep the tone plain, warm, and non-intimidating. Avoid legalese where possible.
- Format the answer in clean Markdown (short paragraphs, bullet points for lists,
  **bold** for key terms/deadlines/amounts) — it will be rendered as Markdown.
- End with 2-3 short, specific follow-up questions the user might naturally ask next.
{language_line}"""

CLARIFY_SYSTEM_PROMPT = """You are a civic/legal-rights assistant. The retrieval
system could not find confident information to answer the user's question.
Instead of guessing, ask ONE short, specific clarifying question that would help
narrow down what they need (e.g. which state, what kind of dispute, what document
they have, which department is involved).

Respond with ONLY a JSON object, no other text, in this exact shape:
{{"question": "<the question, 1-2 sentences>", "options": ["<short option>", "<short option>", "<short option>"]}}

Rules for options:
- 2-4 options, each 2-6 words, mutually exclusive, covering the most likely
  concrete answers a user in this situation would give.
- Do NOT include a catch-all like "Other" or "Not sure" — the interface always
  offers a free-text option automatically, so you don't need to.
- Do not apologize excessively in the question text.
- Write the "question" and "options" text itself in {language_name}.
"""

SIMPLIFY_SYSTEM_PROMPT = """You are rewriting a previous answer from a civic/legal-
rights assistant so a reader with a **much simpler reading level** (think: a tired
adult reading on their phone, not a lawyer) can follow it easily.

Rules:
- Do NOT do new research or invent facts — only re-express what the ORIGINAL ANSWER
  already said, using the same numbered source citations [1], [2] where the
  original used them.
- Use short sentences (under ~15 words each). Prefer everyday words over legal terms;
  if a legal term is unavoidable, define it in 3-5 plain words right after it.
- Use bullet points instead of dense paragraphs wherever it helps.
- Keep it noticeably shorter than the original — cut anything not essential to
  understanding what to do next.
- Format as clean Markdown.
{language_line}"""


def _language_name(language: str | None) -> str:
    return SUPPORTED_LANGUAGES.get(language or DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES[DEFAULT_LANGUAGE])


def _language_line(language: str | None) -> str:
    name = _language_name(language)
    if (language or DEFAULT_LANGUAGE) == "en":
        return "\n- Respond in clear, simple English."
    return (
        f"\n- Respond entirely in {name}. Use everyday spoken vocabulary, not "
        f"formal literary Hindi, so a non-expert reader finds it natural."
    )


def _format_sources(chunks) -> str:
    """Accepts either RetrievedChunk objects (have .text/.source/.source_tier)
    or plain dicts with the same keys (used when replaying stored chunks for
    the simplify endpoint, which has no live RetrievalResult)."""
    lines = []
    for i, c in enumerate(chunks, start=1):
        text = c.text if hasattr(c, "text") else c["text"]
        source = c.source if hasattr(c, "source") else c["source"]
        tier = c.source_tier if hasattr(c, "source_tier") else c["source_tier"]
        lines.append(f"[{i}] ({tier}, source: {source})\n{text}")
    return "\n\n".join(lines)


def _citations_for(chunks) -> list[dict]:
    out = []
    for i, c in enumerate(chunks, start=1):
        source = c.source if hasattr(c, "source") else c["source"]
        tier = c.source_tier if hasattr(c, "source_tier") else c["source_tier"]
        out.append({"index": i, "source": source, "tier": tier})
    return out


def _build_answer_prompt(user_query: str, retrieval: RetrievalResult, evidence_context: str | None):
    sources_block = _format_sources(retrieval.chunks)
    evidence_block = f"\n\nUser-provided document context:\n{evidence_context}" if evidence_context else ""
    user_prompt = f"""User question: {user_query}

Numbered sources:
{sources_block}{evidence_block}

Answer the user's question, citing sources by their [n] number."""
    return user_prompt


def extract_followup_options(text: str) -> list[str]:
    """
    Extracts 2-3 follow-up questions from the end of an answer markdown text
    (e.g., questions listed under headings like 'What you might want to ask next:').
    """
    options = []
    # Search for bullet points at the end of the text or under follow-up section
    lines = text.split("\n")
    in_followup_section = False
    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()
        if any(h in lower for h in ["what you might", "ask next", "follow-up", "suggested questions", "next steps"]):
            in_followup_section = True
            continue
        if in_followup_section and (stripped.startswith("- ") or stripped.startswith("* ") or re.match(r"^\d+\.\s", stripped)):
            # Clean up bullet / numbering prefix
            item = re.sub(r"^(?:[\-\*]|\d+\.)\s*", "", stripped).strip()
            item = item.strip('*_`"')
            if item and len(item) > 5 and len(item) < 120:
                options.append(item)
    
    # Fallback: if no explicit section header, extract last 2-3 bullet items if they end with ?
    if not options:
        bullet_items = [
            re.sub(r"^(?:[\-\*]|\d+\.)\s*", "", line.strip()).strip('*_`"')
            for line in lines if (line.strip().startswith("- ") or line.strip().startswith("* ") or re.match(r"^\d+\.\s", line.strip()))
        ]
        options = [b for b in bullet_items if b.endswith("?") and len(b) < 120][-3:]
        
    return options[:3]


def generate_answer(user_query: str, retrieval: RetrievalResult,
                     evidence_context: str | None = None, language: str = DEFAULT_LANGUAGE) -> dict:
    """Non-streaming variant — kept for the plain /chat endpoint and for any
    callers that don't need token-by-token output."""
    if not retrieval.confident:
        return _generate_clarify(user_query, language)

    if not llm_client.available():
        fallback_text = "\n\n".join(
            f"[{i+1}] ({c.source_tier}) {c.text[:300]}..."
            for i, c in enumerate(retrieval.chunks)
        )
        return {
            "type": "answer",
            "text": f"({llm_client.NOT_CONFIGURED_MSG})\n\n{fallback_text}",
            "citations": _citations_for(retrieval.chunks),
            "options": [],
        }

    system = ANSWER_SYSTEM_PROMPT.format(language_line=_language_line(language))
    user_prompt = _build_answer_prompt(user_query, retrieval, evidence_context)
    text = llm_client.complete(system, user_prompt, max_tokens=2048)
    options = extract_followup_options(text)

    return {
        "type": "answer",
        "text": text,
        "citations": _citations_for(retrieval.chunks),
        "options": options,
    }


def generate_answer_stream(user_query: str, retrieval: RetrievalResult,
                            evidence_context: str | None = None,
                            language: str = DEFAULT_LANGUAGE) -> Iterator[str]:
    """Streaming variant used by the /chat/stream SSE endpoint. Caller is
    responsible for accumulating the yielded pieces and for building
    citations from `retrieval.chunks` itself (they don't depend on the
    model's output)."""
    system = ANSWER_SYSTEM_PROMPT.format(language_line=_language_line(language))
    user_prompt = _build_answer_prompt(user_query, retrieval, evidence_context)
    yield from llm_client.stream(system, user_prompt, max_tokens=2048)


def simplify_answer_stream(user_query: str, previous_answer: str, stored_chunks: list[dict],
                            language: str = DEFAULT_LANGUAGE) -> Iterator[str]:
    """Regenerates the last answer at a lower reading level, reusing the
    chunks retrieved last turn — no new retrieval, per the architecture doc."""
    system = SIMPLIFY_SYSTEM_PROMPT.format(language_line=_language_line(language))
    sources_block = _format_sources(stored_chunks) if stored_chunks else "(no sources were used in the original answer)"
    user_prompt = f"""Original user question: {user_query}

Numbered sources used originally:
{sources_block}

ORIGINAL ANSWER (to simplify, not to re-derive from scratch):
{previous_answer}

Rewrite the original answer at a much simpler reading level, per your instructions."""
    yield from llm_client.stream(system, user_prompt, max_tokens=1500)


def _extract_json(text: str) -> dict | None:
    """Best-effort JSON extraction in case the model wraps it in prose/fences."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _generate_clarify(user_query: str, language: str = DEFAULT_LANGUAGE) -> dict:
    fallback = {
        "type": "clarify",
        "text": "I couldn't find a confident answer in my sources yet — "
                 "could you share a bit more detail (e.g. which state, or "
                 "what document you have) so I can look this up correctly?",
        "citations": [],
        "options": [],
    }

    if not llm_client.available():
        return fallback

    system = CLARIFY_SYSTEM_PROMPT.format(language_name=_language_name(language))
    raw = llm_client.complete(system, f"User question: {user_query}", max_tokens=300)
    parsed = _extract_json(raw)

    if not parsed or "question" not in parsed:
        # Model didn't return valid JSON — degrade gracefully to plain text,
        # no options, rather than breaking the response.
        return {**fallback, "text": raw or fallback["text"]}

    options = parsed.get("options") or []
    if not isinstance(options, list):
        options = []
    options = [str(o).strip() for o in options if str(o).strip()][:4]

    return {
        "type": "clarify",
        "text": str(parsed["question"]).strip(),
        "citations": [],
        "options": options,
    }


def cross_check_uploaded_law(claim_text: str, retrieval: RetrievalResult) -> dict:
    """
    For "this is the law" uploads: check whether the user's claimed legal text
    is supported by our verified vector DB results. Returns a verdict used to
    label the claim as verified / unconfirmed in the final answer.
    """
    verified_chunks = [c for c in retrieval.chunks if c.source_tier == "verified"]
    if not verified_chunks or not llm_client.available():
        return {"verdict": "unconfirmed", "reason": "No matching verified source found."}

    sources_block = _format_sources(verified_chunks)
    prompt = f"""A user uploaded a document claiming the following about the law:
"{claim_text}"

Compare it against these verified sources:
{sources_block}

Does the claim match, partially match, or contradict the verified sources?
Answer in one short sentence starting with MATCH:, PARTIAL:, or CONTRADICT:."""

    text = llm_client.complete("You are a precise legal fact-checker.", prompt, max_tokens=100)
    verdict = "verified" if text.upper().startswith("MATCH") else (
        "partial" if text.upper().startswith("PARTIAL") else "unconfirmed"
    )
    return {"verdict": verdict, "reason": text}
