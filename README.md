# Nyaya Sahayak — AI Civic & Legal Empowerment Assistant

Working scaffold for PS3 (IIIT Allahabad hackathon). Multi-domain knowledge base
(RTI, consumer protection, labour rights, one welfare scheme) with a session-aware
chat pipeline: query rewriting → retrieval → confidence-gated answer/clarify →
web fallback → grounded, streamed answer + document draft — with a bilingual
(English/Hindi), voice-enabled, "advocate's consultation room" themed UI.

## ⚠️ Before you demo

The files in `data/raw/` are **illustrative summaries written for this scaffold**,
not verified legal text. Replace them with actual Act text / official portal content
before your demo, or clearly caveat in your presentation that this is placeholder
content for architecture demonstration. Do not present these as verbatim law.

## Setup

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file (see `.env.example`):

```
LLM_API_KEY=your_key_here
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=llama-3.3-70b-versatile
TAVILY_API_KEY=your_key_here      # optional, enables web fallback
```

### Switching LLM providers

Every LLM call in this repo goes through `app/llm_client.py`, a thin wrapper
around the OpenAI SDK pointed at `LLM_BASE_URL`. Groq, OpenAI, OpenRouter,
Together, Fireworks, and most self-hosted gateways (vLLM/Ollama in
OpenAI-compat mode) all expose this same `/chat/completions` shape — so
changing providers or models is **only** an edit to `LLM_API_KEY` /
`LLM_BASE_URL` / `LLM_MODEL` in `.env`, then a backend restart. No code in
`app/generator.py`, `app/query_rewriter.py`, or `app/documents.py` needs to
change. See the commented examples in `.env.example`.

## Build the knowledge base (run once, offline)

```bash
python ingest/ingest.py
```

This chunks everything in `data/raw/`, embeds it with a local sentence-transformer
model (no API key needed, downloads once from Hugging Face), and stores it in a
local Chroma DB at `data/chroma/`.

## Run the backend

```bash
uvicorn app.main:app --reload --port 8000
```

## Try it

Open `landing/index.html` for the overview page, or go straight to
`frontend/index.html` for the consultation room. Both are static files —
open them directly in a browser (the app calls `http://localhost:8000`).
The landing page's "Launch the assistant" buttons link straight into
`frontend/index.html`.

## Project layout

```
data/raw/              multi-domain seed legal text (replace before demo)
data/chroma/            generated vector DB (created by ingest.py)
ingest/ingest.py         offline chunk + embed script
app/config.py            env/config loading (LLM provider, thresholds, languages)
app/llm_client.py         provider-agnostic OpenAI-compatible client (complete + stream)
app/vectorstore.py        Chroma wrapper (add/query)
app/query_rewriter.py      resolves "tell me more" style follow-ups using session history
app/retriever.py           retrieval + confidence scoring + Tavily fallback
app/generator.py           answer generation (streaming + non-streaming), clarify,
                            "explain simpler", citations + source-tier labels
app/documents.py           RTI / complaint document drafting templates + fill logic
app/main.py               FastAPI app: /chat, /chat/stream, /simplify/stream,
                            /upload, /draft, /health, /session/{id}
landing/index.html         marketing / overview page, links into frontend/
frontend/index.html        the chat UI — streaming markdown, EN/HI toggle, voice input
```

## Pipeline (matches the architecture doc)

1. `POST /chat/stream` receives `{session_id, message, language}` and opens an
   SSE stream (`POST /chat` also exists, non-streaming, for simple integrations).
2. `query_rewriter.py` turns the message into a standalone query using stored
   session history (fixes "tell me more" repeating the same retrieval).
3. `retriever.py` searches the local vector DB. If the best match score is below
   `CONFIDENCE_THRESHOLD` (see `app/config.py`), it falls back to Tavily web search
   (if `TAVILY_API_KEY` is set) and tags those results as `web-sourced`.
4. If confidence is still low / sources conflict, the stream emits a `clarify`
   turn with tappable options instead of an answer.
5. Otherwise `generator.py` streams the chat answer token-by-token, in the
   requested language, with inline citations and a `source_tier` per claim
   (`verified` / `web-sourced` / `unconfirmed`). The frontend renders it as
   Markdown as it arrives.
6. `POST /simplify/stream` ("Explain simpler" button) re-expresses the LAST
   answer at a lower reading level, reusing the chunks retrieved for that
   turn — no new retrieval, per the architecture doc.
7. `POST /draft` takes the current session context and produces a filled RTI
   application or complaint letter via `documents.py`.
8. `POST /upload` accepts a user document (evidence or "the law"); text is
   extracted and either merged into session facts (evidence) or cross-checked
   against retrieval results (law claims) before being trusted.

## Feature status

- **Real:** chunking, local embeddings, Chroma retrieval, confidence gating,
  session memory, query rewriting, streaming SSE answers rendered as live
  Markdown, English/Hindi language toggle (drives actual LLM output language,
  not just UI copy), voice input (Web Speech API, EN/HI locale-aware),
  "Explain simpler" (reuses prior retrieval, no re-query), document templating,
  hover tooltips explaining evidence vs. "law claim" uploads.
- **Needs your API key:** the actual LLM calls (`LLM_API_KEY` — see provider
  section above) and the Tavily web fallback in `retriever.py`.
- **Not implemented:** OCR for scanned uploads (currently assumes
  text-extractable PDFs/text).
