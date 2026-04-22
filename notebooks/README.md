# Embedded path (no app required)

This directory hosts the alternative deployment: a single Databricks notebook that adds exact + semantic caching directly to the customer's existing `query_genie()` function. No FastAPI app, no service principals, no separate deploy — just import into the workspace, run top-to-bottom.

## Files

- **`genie_cache_embedded.py`** — Databricks source notebook (importable via `databricks workspace import`).

## What it does

1. Installs deps (`psycopg`, `pgvector`, `databricks-sdk`, `pandas`).
2. Reads config from env vars / inline values (instance name, database, Genie space, tuning knobs).
3. **Resolves or creates the Lakebase instance.** If `LAKEBASE_INSTANCE_NAME` already exists, grabs its hostname. If not and `CREATE_INSTANCE_IF_MISSING = True`, creates it at `LAKEBASE_CAPACITY` and polls until `AVAILABLE`.
4. **Creates the cache database** inside the instance if missing.
5. Bootstraps the `cache.*` schema (idempotent `CREATE EXTENSION vector`, tables, HNSW + partial indexes).
6. Shows the customer's **original `query_genie`** verbatim for reference.
7. Defines the caching helpers (`OAuthConnection`, pool, `_check_exact`, `_check_semantic`, `_write_cache`, `cleanup_expired`).
8. Replaces `query_genie` with a cache-aware version — same signature and return dict, plus a new `cache_source` field.
9. Runs a demo: same question twice (exact hit), then a paraphrase (semantic hit).

## Import into a workspace

```bash
databricks workspace import \
  /Users/<you@company.com>/genie_cache_embedded \
  --file notebooks/genie_cache_embedded.py \
  --format SOURCE \
  --language PYTHON \
  --overwrite \
  --profile <PROFILE>
```

## When to pick this over the app-based plugin

- You don't want to operate a separate FastAPI app.
- Your client code already has a `WorkspaceClient` and it's easiest to just drop caching next to it.
- Latency between your code and Lakebase is not a concern (co-located compute).

The app-based path (`skills/genie-cache-install`) is better when you have many callers (the cache is shared via HTTP) or you want cache state decoupled from any one notebook/job.
