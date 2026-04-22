import os
import copy
import hashlib
import datetime as dt
from typing import Any
from psycopg.types.json import Jsonb
from .db import pool


SIMILARITY_THRESHOLD = float(os.environ.get("SEMANTIC_SIMILARITY_THRESHOLD", "0.93"))
# Rows expire CACHE_TTL_SECONDS after write. 0 disables TTL (rows never expire).
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "0"))
# Cap the row count inlined into the cached query_result. 0 = unlimited.
CACHE_MAX_RESULT_ROWS = int(os.environ.get("CACHE_MAX_RESULT_ROWS", "0"))


def normalize_question(q: str) -> str:
    return " ".join(q.strip().lower().split())


def question_hash(q: str) -> str:
    return hashlib.sha256(normalize_question(q).encode("utf-8")).hexdigest()


def _expires_at() -> dt.datetime | None:
    if CACHE_TTL_SECONDS <= 0:
        return None
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=CACHE_TTL_SECONDS)


def bootstrap_schema() -> None:
    """Idempotent migration — safe to run on every startup.

    Adds expires_at columns and supporting indexes if missing. Does not create
    the base tables (those are provisioned separately when the Lakebase
    instance is first set up).
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE cache.exact_cache "
                "ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ"
            )
            cur.execute(
                "ALTER TABLE cache.semantic_cache "
                "ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS exact_cache_expires_idx "
                "ON cache.exact_cache (expires_at) "
                "WHERE expires_at IS NOT NULL"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS semantic_cache_expires_idx "
                "ON cache.semantic_cache (expires_at) "
                "WHERE expires_at IS NOT NULL"
            )
        conn.commit()


def _truncate_result_rows(response: dict[str, Any]) -> dict[str, Any]:
    """Cap data_array to CACHE_MAX_RESULT_ROWS. Tags the statement with
    _cached_truncated=True when it actually shortened anything so the UI can
    tell the user the cached table is a subset.
    """
    if CACHE_MAX_RESULT_ROWS <= 0:
        return response
    try:
        stmt = response["query_result"]["statement_response"]
        data = stmt["result"]["data_array"]
    except (KeyError, TypeError):
        return response
    if not isinstance(data, list) or len(data) <= CACHE_MAX_RESULT_ROWS:
        return response
    new_resp = dict(response)
    new_resp["query_result"] = copy.deepcopy(response["query_result"])
    new_result = new_resp["query_result"]["statement_response"]["result"]
    new_result["data_array"] = data[:CACHE_MAX_RESULT_ROWS]
    new_result["_cached_truncated"] = True
    new_result["_cached_row_count"] = CACHE_MAX_RESULT_ROWS
    return new_resp


def check_exact_cache(space_id: str, question: str) -> dict[str, Any] | None:
    qh = question_hash(question)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE cache.exact_cache
                   SET hit_count = hit_count + 1,
                       last_hit_at = NOW()
                 WHERE question_hash = %s
                   AND genie_space_id = %s
                   AND (expires_at IS NULL OR expires_at > NOW())
                RETURNING response_json, hit_count
                """,
                (qh, space_id),
            )
            row = cur.fetchone()
        conn.commit()
    if not row:
        return None
    response_json, hit_count = row
    return {"response": response_json, "hit_count": hit_count}


def check_semantic_cache(
    space_id: str, embedding: list[float]
) -> dict[str, Any] | None:
    """Return best match above similarity threshold, or None."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # cosine distance = 1 - cosine similarity; lower is better
            cur.execute(
                """
                SELECT id, question, response_json,
                       1 - (embedding <=> %s::vector) AS similarity
                  FROM cache.semantic_cache
                 WHERE genie_space_id = %s
                   AND (expires_at IS NULL OR expires_at > NOW())
                 ORDER BY embedding <=> %s::vector
                 LIMIT 1
                """,
                (embedding, space_id, embedding),
            )
            row = cur.fetchone()
            if not row:
                return None
            row_id, matched_q, response_json, similarity = row
            if similarity is None or similarity < SIMILARITY_THRESHOLD:
                return None
            cur.execute(
                """
                UPDATE cache.semantic_cache
                   SET hit_count = hit_count + 1, last_hit_at = NOW()
                 WHERE id = %s
                """,
                (row_id,),
            )
        conn.commit()
    return {
        "response": response_json,
        "matched_question": matched_q,
        "similarity": float(similarity),
    }


def write_caches(
    space_id: str,
    question: str,
    embedding: list[float],
    response: dict[str, Any],
) -> None:
    truncated = _truncate_result_rows(response)
    qh = question_hash(question)
    payload = Jsonb(truncated)
    expires = _expires_at()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cache.exact_cache
                    (question_hash, question, response_json, genie_space_id, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (question_hash) DO UPDATE
                   SET response_json = EXCLUDED.response_json,
                       last_hit_at = NOW(),
                       expires_at = EXCLUDED.expires_at
                """,
                (qh, question, payload, space_id, expires),
            )
            cur.execute(
                """
                INSERT INTO cache.semantic_cache
                    (question, response_json, embedding, genie_space_id, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (question, payload, embedding, space_id, expires),
            )
        conn.commit()


def cleanup_expired() -> dict[str, int]:
    """Delete all rows past expires_at. Safe to call concurrently."""
    if CACHE_TTL_SECONDS <= 0:
        return {"exact_deleted": 0, "semantic_deleted": 0, "ttl_seconds": 0}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM cache.exact_cache "
                "WHERE expires_at IS NOT NULL AND expires_at <= NOW()"
            )
            exact_deleted = cur.rowcount
            cur.execute(
                "DELETE FROM cache.semantic_cache "
                "WHERE expires_at IS NOT NULL AND expires_at <= NOW()"
            )
            sem_deleted = cur.rowcount
        conn.commit()
    return {
        "exact_deleted": int(exact_deleted or 0),
        "semantic_deleted": int(sem_deleted or 0),
        "ttl_seconds": CACHE_TTL_SECONDS,
    }


def get_cache_stats() -> dict[str, Any]:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*), COALESCE(SUM(hit_count),0), "
                "COUNT(*) FILTER (WHERE expires_at IS NOT NULL AND expires_at <= NOW()) "
                "FROM cache.exact_cache"
            )
            exact_rows, exact_hits, exact_expired = cur.fetchone()
            cur.execute(
                "SELECT COUNT(*), COALESCE(SUM(hit_count),0), "
                "COUNT(*) FILTER (WHERE expires_at IS NOT NULL AND expires_at <= NOW()) "
                "FROM cache.semantic_cache"
            )
            sem_rows, sem_hits, sem_expired = cur.fetchone()
    return {
        "exact_cache": {
            "rows": int(exact_rows),
            "hits": int(exact_hits),
            "expired": int(exact_expired),
        },
        "semantic_cache": {
            "rows": int(sem_rows),
            "hits": int(sem_hits),
            "expired": int(sem_expired),
        },
        "similarity_threshold": SIMILARITY_THRESHOLD,
        "ttl_seconds": CACHE_TTL_SECONDS,
        "max_result_rows": CACHE_MAX_RESULT_ROWS,
    }
