"""Conversation storage worker. It runs as the wallet user with only its own volume."""
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
import uuid
from contextlib import closing

MAX_CHATS = 256
MAX_TITLE = 120
MAX_PROMPT = 64 * 1024
MAX_PACKET = 256 * 1024
ID_RE = re.compile(r"[0-9a-f]{32}")
SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    updated_at REAL NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    share_token TEXT,
    snapshot TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    PRIMARY KEY(conversation_id, revision),
    UNIQUE(conversation_id, request_id)
);
"""


class StoreError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _summary(row):
    return {
        "id": row["id"], "title": row["title"], "updated_at": row["updated_at"],
        "revision": row["revision"], "shared": bool(row["shared"]),
    }


def _row(db, cid):
    row = db.execute("SELECT *, share_token IS NOT NULL AS shared FROM conversations WHERE id=?", (cid,)).fetchone()
    if row is None:
        raise StoreError("Conversation unavailable.", 404)
    return row


def _detail(db, row):
    result = _summary(row)
    result["share_url"] = "/intelligence/share/" + row["share_token"] if row["share_token"] else None
    result["messages"] = []
    for question, answer in db.execute(
        "SELECT question, answer FROM turns WHERE conversation_id=? ORDER BY revision", (row["id"],)
    ):
        result["messages"].extend(({"role": "user", "content": question}, {"role": "assistant", "content": answer}))
    return result


def _turn(db, row, operation, data):
    cid, expected = row["id"], data.get("revision")
    question, request_id = data.get("prompt"), data.get("request_id")
    if not isinstance(question, str) or not question.strip() or type(expected) is not int or not 0 <= expected <= 1000000:
        raise StoreError("Enter a message and its current conversation revision.")
    try:
        if str(uuid.UUID(request_id)) != request_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise StoreError("Invalid message request ID.") from None
    question = question.strip()
    previous = db.execute(
        "SELECT revision, question FROM turns WHERE conversation_id=? AND request_id=?", (cid, request_id)
    ).fetchone()
    if previous and (previous["question"], previous["revision"]) == (question, expected + 1):
        result = _detail(db, row)
        return ({"conversation": result} if operation == "prepare" else result), 200
    if previous or expected != row["revision"]:
        raise StoreError("This conversation changed. Reopen it before sending again.", 409)
    if operation == "prepare":
        parts = []
        for user, assistant in db.execute(
            "SELECT question, answer FROM turns WHERE conversation_id=? ORDER BY revision", (cid,)
        ):
            parts.extend(("User: " + user, "AxonAI: " + assistant))
        parts.append("User: " + question if parts else question)
        prompt = "\n\n".join(parts)
        if len(json.dumps({"prompt": prompt}, ensure_ascii=False).encode()) > MAX_PROMPT:
            raise StoreError("The conversation exceeds 64 KiB. Start a new chat.", 413)
        return {"prompt": prompt}, 200
    answer = data.get("answer")
    if not isinstance(answer, str) or len(answer.encode()) > MAX_PROMPT:
        raise StoreError("Invalid reply.", 502)
    db.execute("INSERT INTO turns VALUES (?, ?, ?, ?, ?)", (cid, expected + 1, request_id, question, answer))
    title = " ".join(question.split())[:80] if expected == 0 and row["title"] == "New chat" else row["title"]
    db.execute("UPDATE conversations SET revision=revision+1, updated_at=?, title=? WHERE id=?", (time.time(), title, cid))
    return _detail(db, _row(db, cid)), 200


def _execute(db, operation, data):
    if operation == "list":
        query = data.get("q", "")
        if not isinstance(query, str) or len(query) > 200:
            raise StoreError("Search is limited to 200 characters.")
        query = query.casefold()
        rows = db.execute("""
            SELECT id,title,updated_at,revision,share_token IS NOT NULL AS shared FROM conversations c
            WHERE ?='' OR instr(casefold(title),?) OR EXISTS (
              SELECT 1 FROM turns WHERE conversation_id=c.id AND
              (instr(casefold(question),?) OR instr(casefold(answer),?)))
            ORDER BY updated_at DESC, id LIMIT ?
        """, (query, query, query, query, MAX_CHATS))
        return {"conversations": [_summary(r) for r in rows], "storage_available": True}, 200
    if operation == "create":
        if db.execute("SELECT count(*) FROM conversations").fetchone()[0] >= MAX_CHATS:
            raise StoreError(f"You have {MAX_CHATS} conversations. Delete one to start another.", 409)
        cid = uuid.uuid4().hex
        db.execute("INSERT INTO conversations(id,title,updated_at) VALUES (?, 'New chat', ?)", (cid, time.time()))
        return _detail(db, _row(db, cid)), 201
    cid = data.get("id")
    if not isinstance(cid, str) or not ID_RE.fullmatch(cid):
        raise StoreError("Conversation unavailable.", 404)
    row = _row(db, cid)
    if operation == "get":
        return _detail(db, row), 200
    if operation == "delete":
        db.execute("DELETE FROM conversations WHERE id=?", (cid,))
        return {}, 200
    if operation == "rename":
        title = data.get("title")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > MAX_TITLE:
            raise StoreError(f"Enter a title of 1 to {MAX_TITLE} characters.")
        db.execute("UPDATE conversations SET title=?,updated_at=? WHERE id=?", (title.strip(), time.time(), cid))
        return _detail(db, _row(db, cid)), 200
    if operation in ("prepare", "append"):
        return _turn(db, row, operation, data)
    if operation == "public":
        token = data.get("token")
        if not row["share_token"] or not isinstance(token, str) or not secrets.compare_digest(token, row["share_token"]):
            raise StoreError("Shared conversation unavailable.", 404)
        return json.loads(row["snapshot"]), 200
    if operation == "revoke":
        db.execute("UPDATE conversations SET share_token=NULL,snapshot=NULL WHERE id=?", (cid,))
        return {}, 200
    if operation == "share":
        token = row["share_token"]
        if not token:
            if not row["revision"]:
                raise StoreError("Send a message before sharing this conversation.", 409)
            token = data["token"]
            snapshot = {"title": row["title"], "messages": _detail(db, row)["messages"]}
            db.execute("UPDATE conversations SET share_token=?,snapshot=? WHERE id=?",
                       (token, json.dumps(snapshot, ensure_ascii=False), cid))
        return {"url": "/intelligence/share/" + token}, 200
    raise StoreError("Invalid storage operation.")


def dispatch(root, operation, data):
    try:
        directory = Path(root) / ".axonos-intelligence"
        if operation == "public" and not directory.is_dir():
            raise StoreError("Shared conversation unavailable.", 404)
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise StoreError("Conversation storage is unavailable.", 503)
        # mode=rwc for owner operations. A public link never initializes storage.
        mode = "ro" if operation == "public" else "rwc"
        path = directory / "history.sqlite3"
        if path.is_symlink():
            raise StoreError("Conversation storage is unavailable.", 503)
        if operation == "public" and not path.is_file():
            raise StoreError("Shared conversation unavailable.", 404)
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=" + mode, uri=True, timeout=4)) as db:
            db.row_factory = sqlite3.Row
            db.create_function("casefold", 1, str.casefold, deterministic=True)
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA foreign_keys=ON")
            if operation != "public":
                db.executescript(SCHEMA)
            with db:
                db.execute("BEGIN" if operation in ("list", "get", "prepare", "public") else "BEGIN IMMEDIATE")
                return _execute(db, operation, data)
    except StoreError as exc:
        return {"error": str(exc)}, exc.status
    except (sqlite3.Error, OSError, UnicodeError, ValueError):
        return {"error": "Conversation storage is unavailable. Try again shortly."}, 503


if __name__ == "__main__":
    import signal
    # The worker exits even if its client disconnects during a slow operation.
    signal.alarm(15)
    os.umask(0o077)
    packet = json.loads(sys.stdin.buffer.read(MAX_PACKET + 1))
    result, status = dispatch("/wallet", packet["operation"], packet["data"])
    output = json.dumps([result, status], ensure_ascii=False)
    if len(output.encode("utf-8")) > 1024 * 1024:
        output = json.dumps([{"error": "Conversation data exceeded the storage limit."}, 503])
    print(output)
