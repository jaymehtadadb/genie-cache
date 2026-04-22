import os
import time
import requests
from typing import Any
from .config import get_oauth_token, get_workspace_host


POLL_INTERVAL = float(os.environ.get("GENIE_POLL_INTERVAL_SECONDS", "1.0"))
POLL_TIMEOUT = float(os.environ.get("GENIE_POLL_TIMEOUT_SECONDS", "120"))


class GenieError(Exception):
    pass


def _headers(user_token: str | None = None) -> dict:
    token = user_token or get_oauth_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _base() -> str:
    return f"{get_workspace_host()}/api/2.0/genie"


def ask_genie(
    space_id: str, question: str, user_token: str | None = None
) -> dict[str, Any]:
    """Start a new conversation in the Genie space, poll until the message
    completes, then return a structured response.

    Returns:
        dict with keys:
          - conversation_id: str
          - message_id: str
          - text: str (Genie's natural-language answer, if any)
          - attachments: list (raw attachments from Genie)
          - query: dict or None (SQL query + description, if one was generated)
    """
    base = _base()

    # Start conversation (also sends the first message)
    start_url = f"{base}/spaces/{space_id}/start-conversation"
    r = requests.post(
        start_url,
        headers=_headers(user_token=user_token),
        json={"content": question},
        timeout=30,
    )
    if not r.ok:
        raise GenieError(f"start-conversation failed: {r.status_code} {r.text}")
    start = r.json()

    conv_id = start["conversation_id"]
    msg_id = start["message_id"]

    # Poll message status
    msg_url = (
        f"{base}/spaces/{space_id}/conversations/{conv_id}/messages/{msg_id}"
    )
    deadline = time.time() + POLL_TIMEOUT
    msg: dict[str, Any] = {}
    while time.time() < deadline:
        m = requests.get(msg_url, headers=_headers(), timeout=30)
        if not m.ok:
            raise GenieError(f"poll message failed: {m.status_code} {m.text}")
        msg = m.json()
        status = msg.get("status")
        if status in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(POLL_INTERVAL)
    else:
        raise GenieError(f"Genie poll timed out after {POLL_TIMEOUT}s")

    if msg.get("status") != "COMPLETED":
        raise GenieError(
            f"Genie returned status={msg.get('status')} error={msg.get('error')}"
        )

    # Extract text + query info from attachments
    text_parts: list[str] = []
    query_info: dict | None = None
    attachments = msg.get("attachments", []) or []
    for att in attachments:
        if "text" in att and att["text"]:
            t = att["text"].get("content")
            if t:
                text_parts.append(t)
        if "query" in att and att["query"]:
            q = att["query"]
            query_info = {
                "description": q.get("description"),
                "query": q.get("query"),
                "statement_id": q.get("statement_id"),
                "query_result_metadata": q.get("query_result_metadata"),
            }

    # If a SQL query was generated, fetch the first page of results
    query_result = None
    if query_info and query_info.get("statement_id"):
        res_url = (
            f"{base}/spaces/{space_id}/conversations/{conv_id}/messages/"
            f"{msg_id}/attachments/{attachments[-1].get('attachment_id','')}/query-result"
        )
        # The attachment_id we need is the one on the query attachment
        for att in attachments:
            if "query" in att and att.get("attachment_id"):
                res_url = (
                    f"{base}/spaces/{space_id}/conversations/{conv_id}/messages/"
                    f"{msg_id}/attachments/{att['attachment_id']}/query-result"
                )
                break
        try:
            rr = requests.get(res_url, headers=_headers(), timeout=60)
            if rr.ok:
                query_result = rr.json()
        except Exception:
            query_result = None

    return {
        "conversation_id": conv_id,
        "message_id": msg_id,
        "text": "\n\n".join(text_parts) if text_parts else None,
        "query": query_info,
        "query_result": query_result,
        "attachments": attachments,
    }
