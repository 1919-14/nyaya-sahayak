"""
FastAPI backend. Run with:
    uvicorn app.main:app --reload --port 8000

Endpoints:
    POST   /chat           — main conversational loop, non-streaming
    POST   /chat/stream    — same pipeline, streams token-by-token over SSE with search status
    POST   /simplify/stream — "Explain simpler": re-expresses last answer at a lower reading level
    POST   /upload         — accept an evidence or "law" document, extract text, merge/cross-check
    POST   /draft          — generate an RTI application or complaint letter from session context
    GET    /sessions       — list all saved chat sessions ordered by latest activity
    GET    /session/{id}   — load session state & message history
    DELETE /session/{id}   — delete a saved session
    GET    /health         — provider/config sanity check for frontend status pill
"""
import io
import json
import uuid

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pypdf import PdfReader

from app import llm_client, db
from app.config import SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE, LLM_MODEL
from app.query_rewriter import rewrite_query
from app.retriever import retrieve
from app.generator import (
    generate_answer,
    generate_answer_stream,
    simplify_answer_stream,
    _generate_clarify,
    cross_check_uploaded_law,
    extract_followup_options,
)
from app.documents import draft_rti, draft_complaint

app = FastAPI(title="Nyaya Sahayak API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten before any real deployment
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/")
def serve_index():
    return FileResponse("frontend/index.html")


# Initialize SQLite database schema on startup
db.init_db()


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str
    language: str = DEFAULT_LANGUAGE
    web_search: bool = False


class ChatResponse(BaseModel):
    session_id: str
    type: str                 # "answer" | "clarify"
    text: str
    citations: list[dict]
    options: list[str] = []   # tappable options when type == "clarify"
    used_web_fallback: bool


class SimplifyRequest(BaseModel):
    session_id: str


class DraftRequest(BaseModel):
    session_id: str
    doc_type: str              # "rti" | "complaint"
    department_or_recipient: str | None = None
    subject: str | None = None
    applicant_name: str | None = None
    applicant_address: str | None = None
    applicant_contact: str | None = None


def _clean_language(language: str | None) -> str:
    return language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _get_or_create_session(session_id: str, language: str = DEFAULT_LANGUAGE) -> dict:
    session = db.get_session(session_id)
    if not session:
        session = db.save_session(session_id, title="New Matter", language=language)
    elif session.get("language") != language:
        session = db.save_session(session_id, language=language)
    return session  # type: ignore


@app.get("/health")
def health():
    return {
        "llm_configured": llm_client.available(),
        "llm_model": LLM_MODEL,
        "supported_languages": SUPPORTED_LANGUAGES,
    }


@app.get("/sessions")
def list_sessions():
    return db.list_sessions()


@app.get("/session/{session_id}")
def get_session(session_id: str):
    sess = db.get_session(session_id)
    if not sess:
        raise HTTPException(404, "Unknown session_id")
    return sess


@app.delete("/session/{session_id}")
def delete_session(session_id: str):
    db.delete_session(session_id)
    return {"status": "deleted", "session_id": session_id}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    language = _clean_language(req.language)
    session = _get_or_create_session(session_id, language)

    # 1. Resolve follow-ups using history
    standalone_query = rewrite_query(req.message, session["history"])

    # 2. Retrieve
    retrieval = retrieve(standalone_query, web_search=req.web_search)

    # 3. Generate answer / clarify
    evidence_context = "\n".join(session["evidence_notes"]) if session["evidence_notes"] else None
    result = generate_answer(standalone_query, retrieval, evidence_context, language)

    # Update session title if default
    if session.get("title") in (None, "New Matter", "Untitled Matter"):
        title = req.message[:45] + ("..." if len(req.message) > 45 else "")
        db.save_session(session_id, title=title, language=language)

    # 4. Save history & messages to SQLite DB
    db.add_message(session_id, "user", req.message)

    citations = result.get("citations", [])
    options = result.get("options", [])
    used_web_fallback = retrieval.used_fallback

    last_retrieval = [
        {"text": c.text, "source": c.source, "source_tier": c.source_tier} for c in retrieval.chunks
    ] if result["type"] == "answer" else []

    db.add_message(
        session_id,
        result["type"],
        result["text"],
        citations=citations,
        options=options,
        used_web_fallback=used_web_fallback,
    )
    db.save_session(
        session_id,
        language=language,
        last_query=standalone_query,
        last_answer=result["text"],
        last_retrieval=last_retrieval,
    )

    return ChatResponse(
        session_id=session_id,
        type=result["type"],
        text=result["text"],
        citations=citations,
        options=options,
        used_web_fallback=used_web_fallback,
    )


@app.post("/chat/stream")
def chat_stream(req: ChatRequest):
    """Same pipeline as /chat, but streams the model's answer over
    Server-Sent Events so the UI can render tokens as they arrive.

    Event shapes (each line is `data: <json>\\n\\n`):
      {"event": "status", "status": "searching_local" | "searching_web"}
      {"event": "start", "session_id", "used_web_fallback"}
      {"event": "token", "text": "<piece>"}
      {"event": "done", "type", "citations", "options", "session_id"}
    """
    session_id = req.session_id or str(uuid.uuid4())
    language = _clean_language(req.language)
    session = _get_or_create_session(session_id, language)

    standalone_query = rewrite_query(req.message, session["history"])
    evidence_context = "\n".join(session["evidence_notes"]) if session["evidence_notes"] else None

    def event_gen():
        # Save user message immediately to SQLite & local state
        db.add_message(session_id, "user", req.message)
        session["history"].append({"role": "user", "content": req.message})

        # Update session title if brand new
        if session.get("title") in (None, "New Matter", "Untitled Matter"):
            title = req.message[:45] + ("..." if len(req.message) > 45 else "")
            db.save_session(session_id, title=title, language=language)

        # Signal local retrieval started
        yield _sse({"event": "status", "status": "searching_local"})

        web_search_triggered = [False]
        def status_cb(status_type: str):
            if status_type == "searching_web":
                web_search_triggered[0] = True

        retrieval = retrieve(standalone_query, web_search=req.web_search, status_callback=status_cb)

        if web_search_triggered[0] or retrieval.used_fallback:
            yield _sse({"event": "status", "status": "searching_web"})

        yield _sse({
            "event": "start",
            "session_id": session_id,
            "used_web_fallback": retrieval.used_fallback,
        })

        if not retrieval.confident:
            clarify = _generate_clarify(standalone_query, language)
            text = clarify["text"]
            for word in text.split(" "):
                yield _sse({"event": "token", "text": word + " "})

            session["history"].append({"role": "assistant", "content": text})
            session["last_query"] = standalone_query
            session["last_answer"] = text
            session["last_retrieval"] = []

            db.add_message(session_id, "clarify", text, citations=[], options=clarify["options"], used_web_fallback=False)
            db.save_session(session_id, language=language, last_query=standalone_query, last_answer=text, last_retrieval=[])

            yield _sse({
                "event": "done",
                "type": "clarify",
                "citations": [],
                "options": clarify["options"],
                "session_id": session_id,
            })
            return

        full_text = ""
        for piece in generate_answer_stream(standalone_query, retrieval, evidence_context, language):
            full_text += piece
            yield _sse({"event": "token", "text": piece})

        citations = [
            {"index": i + 1, "source": c.source, "tier": c.source_tier, "url": getattr(c, "url", None)}
            for i, c in enumerate(retrieval.chunks)
        ]
        last_retrieval = [
            {"text": c.text, "source": c.source, "source_tier": c.source_tier} for c in retrieval.chunks
        ]
        options = extract_followup_options(full_text)

        session["history"].append({"role": "assistant", "content": full_text})
        session["last_query"] = standalone_query
        session["last_answer"] = full_text
        session["last_retrieval"] = last_retrieval

        db.add_message(session_id, "assistant", full_text, citations=citations, options=options, used_web_fallback=retrieval.used_fallback)
        db.save_session(session_id, language=language, last_query=standalone_query, last_answer=full_text, last_retrieval=last_retrieval)

        yield _sse({
            "event": "done",
            "type": "answer",
            "citations": citations,
            "options": options,
            "session_id": session_id,
        })

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.post("/simplify/stream")
def simplify_stream(req: SimplifyRequest):
    session = db.get_session(req.session_id)
    if not session or not session.get("last_answer"):
        raise HTTPException(404, "No previous answer to simplify yet — chat first.")

    language = session.get("language", DEFAULT_LANGUAGE)
    stored_chunks = session.get("last_retrieval", [])
    last_query = session.get("last_query", "")
    last_answer = session["last_answer"]

    def event_gen():
        yield _sse({"event": "start", "session_id": req.session_id})
        full_text = ""
        for piece in simplify_answer_stream(last_query, last_answer, stored_chunks, language):
            full_text += piece
            yield _sse({"event": "token", "text": piece})
        yield _sse({"event": "done", "type": "simplified", "session_id": req.session_id})

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.post("/upload")
async def upload_document(
    session_id: str = Form(...),
    doc_role: str = Form(...),   # "evidence" | "law_claim"
    file: UploadFile = File(...),
):
    if doc_role not in ("evidence", "law_claim"):
        raise HTTPException(400, "doc_role must be 'evidence' or 'law_claim'")

    raw = await file.read()
    text = _extract_text(raw, file.filename)
    if not text.strip():
        raise HTTPException(422, "Could not extract text from this file. OCR for scanned documents isn't wired up yet.")

    session = _get_or_create_session(session_id)
    evidence_notes = session.get("evidence_notes", [])

    if doc_role == "evidence":
        evidence_notes.append(text[:2000])
        db.save_session(session_id, evidence_notes=evidence_notes)
        return {"status": "stored_as_evidence", "extracted_preview": text[:300]}

    retrieval = retrieve(text[:500])
    verdict = cross_check_uploaded_law(text[:1500], retrieval)

    if verdict["verdict"] == "verified":
        evidence_notes.append(f"(Verified against our sources) {text[:1500]}")
    else:
        evidence_notes.append(
            f"(UNCONFIRMED — user-provided, not independently verified: {verdict['reason']}) {text[:500]}"
        )

    db.save_session(session_id, evidence_notes=evidence_notes)
    return {"status": "cross_checked", "verdict": verdict}


@app.post("/draft")
def draft_document(req: DraftRequest):
    session = db.get_session(req.session_id)
    if not session:
        raise HTTPException(404, "Unknown session_id — chat first to build context.")

    conversation_summary = "\n".join(
        f"{t['role']}: {t['content']}" for t in session["history"]
    )
    applicant = {
        "name": req.applicant_name or session["applicant"].get("name"),
        "address": req.applicant_address or session["applicant"].get("address"),
        "contact": req.applicant_contact or session["applicant"].get("contact"),
    }
    db.save_session(req.session_id, applicant=applicant)

    if req.doc_type == "rti":
        text = draft_rti(req.department_or_recipient, conversation_summary, applicant)
    elif req.doc_type == "complaint":
        text = draft_complaint(
            req.department_or_recipient, req.subject, conversation_summary, applicant
        )
    else:
        raise HTTPException(400, "doc_type must be 'rti' or 'complaint'")

    return {"doc_type": req.doc_type, "text": text}


def _extract_text(raw: bytes, filename: str) -> str:
    if filename.lower().endswith(".pdf"):
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="ignore")
