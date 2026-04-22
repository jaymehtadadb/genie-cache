# Databricks notebook source
# MAGIC %md
# MAGIC # Genie Cache — Embedded in Your Code
# MAGIC
# MAGIC This notebook takes your existing `query_genie` function and adds a transparent caching layer in front of the Genie API.
# MAGIC
# MAGIC No app, no extra service — just a Lakebase Postgres database, a one-time DDL, and a handful of helper functions.
# MAGIC
# MAGIC ## Flow
# MAGIC 1. Install deps + set config.
# MAGIC 2. Bootstrap the Lakebase cache schema (one-time, idempotent).
# MAGIC 3. **Your existing `query_genie` function — verbatim, for reference.**
# MAGIC 4. Add the caching helpers (pool, embeddings, lookups, writes).
# MAGIC 5. Replace `query_genie` with the cache-aware version.
# MAGIC 6. Run the demo.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Install dependencies

# COMMAND ----------

# MAGIC %pip install --quiet --upgrade "psycopg[binary,pool]>=3.2.0" pgvector "databricks-sdk>=0.40.0" pandas
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Configuration
# MAGIC
# MAGIC `PG_USER` is auto-derived from the SDK identity — whoever runs this notebook is who the Postgres role will be.

# COMMAND ----------

import os
import databricks.sdk
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()

# Sanity check — cluster runtime sometimes ships an older SDK that shadows the pip install.
print("databricks-sdk version:", databricks.sdk.version.__version__)
assert hasattr(w, "database"), (
    "databricks-sdk is too old (missing w.database). "
    "Re-run cell 1 (it force-upgrades the SDK and restarts Python), then run this cell again."
)

# IMPORTANT: instance name ≠ database name.
#   LAKEBASE_INSTANCE_NAME  → the Lakebase compute instance (hyphens)  e.g. "genie-loadtest-db"
#   PG_DATABASE             → the Postgres database inside it         e.g. "genie_cache_db" (underscores)

# Instance lifecycle
CREATE_INSTANCE_IF_MISSING = False          # set True to auto-create the Lakebase instance
LAKEBASE_CAPACITY          = "CU_1"         # only used on create: CU_1 | CU_2 | CU_4 | CU_8

# Lakebase (PG_HOST is resolved automatically in the next cell from the instance)
os.environ["LAKEBASE_INSTANCE_NAME"] = "genie-loadtest-db"
os.environ["PG_DATABASE"]            = "genie_cache_db"
os.environ["PG_USER"]                = w.current_user.me().user_name   # auto: your email (or SP UUID)

# Genie + embeddings
GENIE_SPACE_ID = "01f11821c34b1783b8c13e2a0c1b752a"
os.environ["EMBEDDING_ENDPOINT"]     = "databricks-gte-large-en"

# Cache knobs
os.environ["SIMILARITY_THRESHOLD"]   = "0.80"
os.environ["CACHE_TTL_SECONDS"]      = "86400"
os.environ["CACHE_MAX_RESULT_ROWS"]  = "100"

print("Running as:", os.environ["PG_USER"])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Resolve or create the Lakebase instance
# MAGIC
# MAGIC Looks up `LAKEBASE_INSTANCE_NAME`. If it exists, grabs its hostname. If it doesn't and `CREATE_INSTANCE_IF_MISSING = True`, creates it at `LAKEBASE_CAPACITY` and polls until state is `AVAILABLE` (usually 2–5 minutes).

# COMMAND ----------

import time

from databricks.sdk.errors import NotFound

def _resolve_or_create_instance(name: str, capacity: str, create: bool, timeout_sec: int = 900):
    try:
        inst = w.database.get_database_instance(name=name)
        print(f"Instance '{name}' exists (state={inst.state}).")
    except NotFound:
        if not create:
            raise RuntimeError(
                f"Lakebase instance '{name}' does not exist. "
                "Set CREATE_INSTANCE_IF_MISSING = True in the config cell to create it."
            )
        print(f"Creating instance '{name}' with capacity {capacity}...")
        from databricks.sdk.service.database import DatabaseInstance
        w.database.create_database_instance(
            database_instance=DatabaseInstance(name=name, capacity=capacity)
        )
        inst = w.database.get_database_instance(name=name)

    # Poll until AVAILABLE
    deadline = time.time() + timeout_sec
    while str(inst.state) != "DatabaseInstanceState.AVAILABLE" and inst.state != "AVAILABLE":
        if time.time() > deadline:
            raise TimeoutError(f"Instance did not reach AVAILABLE within {timeout_sec}s (last state={inst.state}).")
        print(f"  state={inst.state} — waiting...")
        time.sleep(10)
        inst = w.database.get_database_instance(name=name)

    return inst

inst = _resolve_or_create_instance(
    name=os.environ["LAKEBASE_INSTANCE_NAME"],
    capacity=LAKEBASE_CAPACITY,
    create=CREATE_INSTANCE_IF_MISSING,
)
os.environ["PG_HOST"] = inst.read_write_dns
print(f"Instance AVAILABLE. PG_HOST = {os.environ['PG_HOST']}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4a. Create the cache database (one-time)
# MAGIC
# MAGIC Lakebase exposes a built-in management database called `databricks_postgres`. We connect to it first, check whether our target database exists, and `CREATE DATABASE` if not. Skip this cell if the database already exists.

# COMMAND ----------

import psycopg

def _fresh_token(request_id: str = "genie-cache"):
    """Mint a short-lived Postgres OAuth token.
    Uses w.database if available; falls back to the REST endpoint for older SDKs."""
    instance = os.environ["LAKEBASE_INSTANCE_NAME"]
    if hasattr(w, "database"):
        cred = w.database.generate_database_credential(
            instance_names=[instance], request_id=request_id,
        )
        return cred.token
    resp = w.api_client.do(
        "POST",
        "/api/2.0/database/credentials",
        body={"instance_names": [instance], "request_id": request_id},
    )
    return resp["token"]


target_db = os.environ["PG_DATABASE"]
mgmt_conn = psycopg.connect(
    host=os.environ["PG_HOST"], port=5432,
    dbname="databricks_postgres", user=os.environ["PG_USER"],
    password=_fresh_token(), sslmode="require",
    autocommit=True,  # CREATE DATABASE cannot run in a transaction
)
with mgmt_conn.cursor() as cur:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,))
    exists = cur.fetchone() is not None
    if exists:
        print(f"Database '{target_db}' already exists.")
    else:
        cur.execute(f'CREATE DATABASE "{target_db}"')
        print(f"Created database '{target_db}'.")
mgmt_conn.close()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4b. Bootstrap the cache schema (one-time)

# COMMAND ----------

DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS cache;

CREATE TABLE IF NOT EXISTS cache.exact_cache (
    question_hash   TEXT PRIMARY KEY,
    question        TEXT NOT NULL,
    response_json   JSONB NOT NULL,
    genie_space_id  TEXT NOT NULL,
    hit_count       INT  DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_hit_at     TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS cache.semantic_cache (
    id              BIGSERIAL PRIMARY KEY,
    question        TEXT NOT NULL,
    response_json   JSONB NOT NULL,
    embedding       VECTOR(1024) NOT NULL,
    genie_space_id  TEXT NOT NULL,
    hit_count       INT  DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_hit_at     TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS semantic_cache_embedding_hnsw_idx
    ON cache.semantic_cache USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS exact_cache_expires_idx
    ON cache.exact_cache (expires_at) WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS semantic_cache_expires_idx
    ON cache.semantic_cache (expires_at) WHERE expires_at IS NOT NULL;
"""

conn = psycopg.connect(
    host=os.environ["PG_HOST"], port=5432,
    dbname=os.environ["PG_DATABASE"], user=os.environ["PG_USER"],
    password=_fresh_token(), sslmode="require",
)
with conn.cursor() as cur:
    cur.execute(DDL)
conn.commit()
conn.close()
print("Schema ready.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Your existing `query_genie` — verbatim
# MAGIC
# MAGIC This is the code you shared, unchanged. Nothing cached. Every call hits Genie.

# COMMAND ----------

import pandas as pd

def query_genie(question, genie_space, conversation_id=None):
    """Execute a single Genie query using the SDK."""

    if conversation_id is None:
        # Start new conversation and wait for completion
        response = w.genie.start_conversation_and_wait(genie_space, question)
    else:
        # Continue existing conversation
        response = w.genie.create_message_and_wait(genie_space, conversation_id, question)

    output = {
        "conversation_id": response.conversation_id,
        "text": None,
        "description": None,
        "sql": None,
        "data": None
    }

    if response.attachments:
        for att in response.attachments:
            if att.text:
                output["text"] = att.text.content
            if att.query:
                output["description"] = att.query.description
                output["sql"] = att.query.query

    # Get query results if available
    if response.query_result and response.query_result.statement_id:
        result = w.statement_execution.get_statement(response.query_result.statement_id)
        output["data"] = pd.DataFrame(
            result.result.data_array,
            columns=[col.name for col in result.manifest.schema.columns]
        )

    return output

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Add the caching helpers
# MAGIC
# MAGIC These six helpers are all the new code you need. They don't touch your function yet — that happens in section 6.

# COMMAND ----------

import hashlib
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

SIMILARITY_THRESHOLD  = float(os.environ["SIMILARITY_THRESHOLD"])
CACHE_TTL_SECONDS     = int(os.environ["CACHE_TTL_SECONDS"])
CACHE_MAX_RESULT_ROWS = int(os.environ["CACHE_MAX_RESULT_ROWS"])
EMBEDDING_ENDPOINT    = os.environ["EMBEDDING_ENDPOINT"]
LAKEBASE_INSTANCE     = os.environ["LAKEBASE_INSTANCE_NAME"]


class OAuthConnection(psycopg.Connection):
    """Generates a fresh Lakebase OAuth token every time the pool opens a connection."""
    @classmethod
    def connect(cls, conninfo="", **kwargs):
        kwargs["password"] = _fresh_token("genie-cache-client")
        return super().connect(conninfo, **kwargs)


_pool = ConnectionPool(
    conninfo=(
        f"host={os.environ['PG_HOST']} port=5432 "
        f"dbname={os.environ['PG_DATABASE']} user={os.environ['PG_USER']} sslmode=require"
    ),
    connection_class=OAuthConnection,
    min_size=1, max_size=5, max_lifetime=2700,  # recycle before 1h token expiry
    open=True,
)


def _hash(q: str) -> str:
    return hashlib.sha256(q.strip().lower().encode()).hexdigest()


def _embed(text: str) -> list:
    r = w.serving_endpoints.query(name=EMBEDDING_ENDPOINT, input=[text])
    element = r.data[0]
    # Newer SDK: typed object with .embedding; older SDK / raw response: subscriptable dict.
    raw = element.embedding if hasattr(element, "embedding") else element["embedding"]
    # pgvector requires a homogeneous float list; the endpoint can return a mix of int/float.
    return [float(x) for x in raw]


def _expires_at():
    if CACHE_TTL_SECONDS <= 0:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=CACHE_TTL_SECONDS)


def _payload_to_output(payload: dict) -> dict:
    """Rebuild the same dict shape your query_genie returns — from a cached JSONB payload."""
    df = None
    if payload.get("data_array") is not None and payload.get("columns"):
        df = pd.DataFrame(payload["data_array"], columns=payload["columns"])
    return {
        "conversation_id": payload.get("conversation_id"),
        "text":            payload.get("text"),
        "description":     payload.get("description"),
        "sql":             payload.get("sql"),
        "data":            df,
        "cache_source":    payload.get("_cache_source"),
    }


def _check_exact(question, genie_space):
    h = _hash(question)
    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE cache.exact_cache
               SET hit_count = hit_count + 1, last_hit_at = NOW()
             WHERE question_hash = %s AND genie_space_id = %s
               AND (expires_at IS NULL OR expires_at > NOW())
         RETURNING response_json
            """,
            (h, genie_space),
        )
        row = cur.fetchone()
        conn.commit()
    if row:
        payload = dict(row[0])
        payload["_cache_source"] = "exact"
        return payload
    return None


def _check_semantic(question, genie_space):
    vec = _embed(question)
    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, response_json, 1 - (embedding <=> %s::vector) AS sim
              FROM cache.semantic_cache
             WHERE genie_space_id = %s
               AND (expires_at IS NULL OR expires_at > NOW())
          ORDER BY embedding <=> %s::vector
             LIMIT 1
            """,
            (vec, genie_space, vec),
        )
        row = cur.fetchone()
        if not row or row[2] < SIMILARITY_THRESHOLD:
            return None, vec
        cur.execute(
            "UPDATE cache.semantic_cache SET hit_count = hit_count + 1, last_hit_at = NOW() WHERE id = %s",
            (row[0],),
        )
        conn.commit()
    payload = dict(row[1])
    payload["_cache_source"] = "semantic"
    return payload, vec


def _write_cache(question, genie_space, output, embedding):
    rows = output["data"].values.tolist() if output["data"] is not None else None
    cols = list(output["data"].columns) if output["data"] is not None else None
    if rows and len(rows) > CACHE_MAX_RESULT_ROWS:
        rows = rows[:CACHE_MAX_RESULT_ROWS]
    payload = {
        "conversation_id": output["conversation_id"],
        "text":            output["text"],
        "description":     output["description"],
        "sql":             output["sql"],
        "columns":         cols,
        "data_array":      rows,
    }
    exp = _expires_at()
    h = _hash(question)
    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cache.exact_cache
                   (question_hash, question, response_json, genie_space_id, expires_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (question_hash) DO UPDATE SET
                response_json = EXCLUDED.response_json,
                expires_at    = EXCLUDED.expires_at,
                last_hit_at   = NOW()
            """,
            (h, question, Jsonb(payload), genie_space, exp),
        )
        cur.execute(
            """
            INSERT INTO cache.semantic_cache
                   (question, response_json, embedding, genie_space_id, expires_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (question, Jsonb(payload), embedding, genie_space, exp),
        )
        conn.commit()


def cleanup_expired() -> dict:
    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM cache.exact_cache     WHERE expires_at <= NOW()")
        exact = cur.rowcount
        cur.execute("DELETE FROM cache.semantic_cache  WHERE expires_at <= NOW()")
        sem = cur.rowcount
        conn.commit()
    return {"exact_deleted": exact, "semantic_deleted": sem}

print("Cache helpers loaded. Pool open as:", os.environ["PG_USER"])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Replace `query_genie` with the cache-aware version
# MAGIC
# MAGIC Same name, same signature, same return dict (+ a new `cache_source` field).
# MAGIC
# MAGIC **What changed from your original:**
# MAGIC - Added `_check_exact` → `_check_semantic` → SDK fallback flow at the top.
# MAGIC - Added `_write_cache(...)` after a miss, using the existing `output` dict and the embedding we already computed.
# MAGIC - Multi-turn calls (`conversation_id` set) bypass the cache — context makes caching unsafe.
# MAGIC - The original SDK logic is kept, just factored into a helper (`_sdk_query`) so the function reads cleanly.

# COMMAND ----------

def _sdk_query(question, genie_space, conversation_id):
    """Unchanged from your original — the SDK round-trip and response unpacking."""
    if conversation_id is None:
        response = w.genie.start_conversation_and_wait(genie_space, question)
    else:
        response = w.genie.create_message_and_wait(genie_space, conversation_id, question)

    output = {
        "conversation_id": response.conversation_id,
        "text": None,
        "description": None,
        "sql": None,
        "data": None,
    }
    if response.attachments:
        for att in response.attachments:
            if att.text:
                output["text"] = att.text.content
            if att.query:
                output["description"] = att.query.description
                output["sql"] = att.query.query
    if response.query_result and response.query_result.statement_id:
        result = w.statement_execution.get_statement(response.query_result.statement_id)
        output["data"] = pd.DataFrame(
            result.result.data_array,
            columns=[col.name for col in result.manifest.schema.columns],
        )
    return output


def query_genie(question, genie_space, conversation_id=None):
    """Drop-in replacement: exact-cache → semantic-cache → SDK fallback."""

    # Multi-turn: skip cache, keep original behavior
    if conversation_id is not None:
        out = _sdk_query(question, genie_space, conversation_id)
        out["cache_source"] = None
        return out

    # 1. Exact-match cache
    cached = _check_exact(question, genie_space)
    if cached:
        return _payload_to_output(cached)

    # 2. Semantic-match cache
    cached, embedding = _check_semantic(question, genie_space)
    if cached:
        return _payload_to_output(cached)

    # 3. Miss — call Genie, then write both cache layers
    output = _sdk_query(question, genie_space, None)
    try:
        _write_cache(question, genie_space, output, embedding)
    except Exception as e:
        print(f"[genie_cache] write failed (non-fatal): {e}")
    output["cache_source"] = None
    return output

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Demo

# COMMAND ----------

import time

question = "What are the top 5 channels by viewership last month?"

t0 = time.time();  r1 = query_genie(question, GENIE_SPACE_ID);  t1 = time.time()
print(f"Call 1 — cache_source={r1['cache_source']!r}  elapsed={t1 - t0:.2f}s")
if r1["data"] is not None:
    display(r1["data"].head())

# COMMAND ----------

t0 = time.time();  r2 = query_genie(question, GENIE_SPACE_ID);  t1 = time.time()
print(f"Call 2 — cache_source={r2['cache_source']!r}  elapsed={t1 - t0:.2f}s")

# COMMAND ----------

t0 = time.time()
r3 = query_genie("Which 5 channels had the most viewers in the last month?", GENIE_SPACE_ID)
t1 = time.time()
print(f"Paraphrase — cache_source={r3['cache_source']!r}  elapsed={t1 - t0:.2f}s")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Inspect the cache

# COMMAND ----------

with _pool.connection() as conn, conn.cursor() as cur:
    cur.execute("""
        SELECT 'exact' AS layer, COUNT(*) AS rows, SUM(hit_count) AS total_hits,
               COUNT(*) FILTER (WHERE expires_at <= NOW()) AS expired
          FROM cache.exact_cache
         UNION ALL
        SELECT 'semantic', COUNT(*), SUM(hit_count),
               COUNT(*) FILTER (WHERE expires_at <= NOW())
          FROM cache.semantic_cache
    """)
    rows = cur.fetchall()

display(pd.DataFrame(rows, columns=["layer", "rows", "total_hits", "expired"]))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 9. Optional: cleanup expired rows

# COMMAND ----------

cleanup_expired()
