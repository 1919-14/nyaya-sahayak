"""
Resolves messages like "tell me more" or "what about the fee?" into a
standalone query using the session's chat history, so the retriever doesn't
just re-run the same search and return identical chunks.

This is the piece that makes session memory actually work, rather than the
system being a stateless RAG lookup with a chat UI glued on top.

Uses the shared llm_client wrapper (see app/llm_client.py) so this module
never needs to know which provider is behind it.
"""
from app import llm_client

REWRITE_SYSTEM_PROMPT = """You rewrite a user's latest chat message into a
standalone search query for a legal/civic-rights retrieval system. The
retrieval knowledge base is in English, so always output the rewritten query
in English regardless of what language the user wrote in.

Rules:
- If the message already stands alone (e.g. a fresh, specific question), return it
  with only minor cleanup — do not change its meaning.
- If the message references earlier context ("tell me more", "what about the fee",
  "and if they don't reply?"), rewrite it into a full question using the chat
  history, and make sure it asks for INFORMATION NOT ALREADY COVERED in the
  assistant's previous answers, so retrieval surfaces new material.
- Output ONLY the rewritten query text, in English. No preamble, no quotes, no
  explanation.
"""


def rewrite_query(message: str, history: list[dict]) -> str:
    """
    history: list of {"role": "user"|"assistant", "content": str}, most recent last.
    """
    if not llm_client.available():
        # No provider configured — fall back to the raw message so the rest
        # of the pipeline still runs (with reduced follow-up quality).
        return message

    history_text = "\n".join(
        f"{turn['role'].upper()}: {turn['content']}" for turn in history[-6:]
    )

    user_prompt = f"""Chat history so far:
{history_text if history_text else '(no prior history — this is the first message)'}

Latest user message: "{message}"

Rewritten standalone query:"""

    return llm_client.complete(REWRITE_SYSTEM_PROMPT, user_prompt, max_tokens=200)
