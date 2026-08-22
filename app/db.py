"""
SQLite database module for persistent chat sessions and conversation history.

Database file: data/nyaya_sahayak.db
"""
import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "nyaya_sahayak.db")


def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'en',
                evidence_notes TEXT NOT NULL DEFAULT '[]',
                applicant TEXT NOT NULL DEFAULT '{}',
                last_query TEXT DEFAULT '',
                last_answer TEXT DEFAULT '',
                last_retrieval TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                citations TEXT DEFAULT '[]',
                options TEXT DEFAULT '[]',
                used_web_fallback INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
        """)
        conn.commit()


def list_sessions() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.id, s.title, s.language, s.updated_at, s.created_at,
                   (SELECT content FROM messages WHERE session_id = s.id AND role = 'user' ORDER BY id ASC LIMIT 1) as first_msg
            FROM sessions s
            ORDER BY s.updated_at DESC
        """)
        rows = cursor.fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "title": r["title"] or (r["first_msg"][:40] + "..." if r["first_msg"] else "Untitled Matter"),
                "language": r["language"],
                "updated_at": r["updated_at"],
                "created_at": r["created_at"],
            })
        return result


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = cursor.fetchone()
        if not row:
            return None

        # Fetch messages
        cursor.execute("""
            SELECT id, role, content, citations, options, used_web_fallback, created_at
            FROM messages
            WHERE session_id = ?
            ORDER BY id ASC
        """, (session_id,))
        msg_rows = cursor.fetchall()

        history = []
        messages_detail = []
        for m in msg_rows:
            history.append({"role": m["role"], "content": m["content"]})
            messages_detail.append({
                "id": m["id"],
                "role": m["role"],
                "content": m["content"],
                "citations": json.loads(m["citations"] or "[]"),
                "options": json.loads(m["options"] or "[]"),
                "used_web_fallback": bool(m["used_web_fallback"]),
                "created_at": m["created_at"],
            })

        return {
            "id": row["id"],
            "title": row["title"],
            "language": row["language"],
            "evidence_notes": json.loads(row["evidence_notes"] or "[]"),
            "applicant": json.loads(row["applicant"] or "{}"),
            "last_query": row["last_query"] or "",
            "last_answer": row["last_answer"] or "",
            "last_retrieval": json.loads(row["last_retrieval"] or "[]"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "history": history,
            "messages": messages_detail,
        }


def save_session(
    session_id: str,
    title: Optional[str] = None,
    language: str = "en",
    evidence_notes: Optional[List[str]] = None,
    applicant: Optional[Dict[str, Any]] = None,
    last_query: str = "",
    last_answer: str = "",
    last_retrieval: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    now = datetime.utcnow().isoformat()
    existing = get_session(session_id)

    if existing:
        new_title = title if title is not None else existing["title"]
        new_notes = json.dumps(evidence_notes if evidence_notes is not None else existing["evidence_notes"])
        new_applicant = json.dumps(applicant if applicant is not None else existing["applicant"])
        new_last_query = last_query if last_query else existing["last_query"]
        new_last_answer = last_answer if last_answer else existing["last_answer"]
        new_last_retrieval = json.dumps(last_retrieval if last_retrieval is not None else existing["last_retrieval"])

        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE sessions
                SET title = ?, language = ?, evidence_notes = ?, applicant = ?,
                    last_query = ?, last_answer = ?, last_retrieval = ?, updated_at = ?
                WHERE id = ?
            """, (new_title, language, new_notes, new_applicant, new_last_query, new_last_answer, new_last_retrieval, now, session_id))
            conn.commit()
    else:
        new_title = title or "New Matter"
        new_notes = json.dumps(evidence_notes or [])
        new_applicant = json.dumps(applicant or {})
        new_last_retrieval = json.dumps(last_retrieval or [])

        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO sessions (id, title, language, evidence_notes, applicant, last_query, last_answer, last_retrieval, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (session_id, new_title, language, new_notes, new_applicant, last_query, last_answer, new_last_retrieval, now, now))
            conn.commit()

    return get_session(session_id)  # type: ignore


def add_message(
    session_id: str,
    role: str,
    content: str,
    citations: Optional[List[Dict[str, Any]]] = None,
    options: Optional[List[str]] = None,
    used_web_fallback: bool = False,
) -> None:
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO messages (session_id, role, content, citations, options, used_web_fallback, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id,
            role,
            content,
            json.dumps(citations or []),
            json.dumps(options or []),
            1 if used_web_fallback else 0,
            now,
        ))
        cursor.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        conn.commit()


def delete_session(session_id: str) -> None:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
