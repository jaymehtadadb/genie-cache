# genie-cache

Plug-and-play semantic caching proxy for Databricks Genie spaces.

Genie answers are slow (often 10–30 s) because every `/ask` call reruns the NL-to-SQL pipeline. This plugin deploys a FastAPI proxy as a Databricks App that sits in front of any Genie space and serves answers from a Lakebase Postgres cache whenever the incoming question matches a prior one — exactly or semantically.

- **Exact cache** — SHA-256 hash lookup, sub-5 ms.
- **Semantic cache** — pg_vector HNSW cosine search against `databricks-gte-large-en` embeddings, ~30 ms.
- **Miss** — falls through to the Genie Conversations API, then writes both cache layers for next time.
- **TTL + background cleanup** — every cached row carries `expires_at`; a periodic task deletes expired rows.
- **Result-row inlining with a cap** — stores up to `CACHE_MAX_RESULT_ROWS` rows per response so fast-path answers include data, not just SQL.
- **Chat UI + /stats + /admin/cleanup** — everything ships in the same app.

## Architecture

```
Client ── /ask ──▶ FastAPI proxy (Databricks App)
                   ├── exact_cache  (SHA-256 → JSONB)
                   ├── semantic_cache (VECTOR(1024) HNSW)
                   └── Genie Conversations API (fallback)
                         │
                         └─ Lakebase Postgres (cache.*)
```

The proxy checks the exact cache first, then the semantic cache (cosine similarity ≥ `SIMILARITY_THRESHOLD`), and only calls Genie on a miss. Misses are written to both caches with an `expires_at` = now + `CACHE_TTL_SECONDS`. A background task runs every `CACHE_CLEANUP_INTERVAL_SECONDS` and deletes expired rows.

## Prerequisites

- **Databricks workspace** that supports Apps + Lakebase (FE-VM serverless, or any workspace with Lakebase enabled).
- **Genie space** already built against your data. You'll need its space ID.
- **Local tooling**:
  - `databricks` CLI 0.229.0+ (`brew upgrade databricks`).
  - `yq` (`brew install yq`).
  - `psql` from `postgresql@16` (`brew install postgresql@16`).
- **Authenticated CLI profile** for the target workspace:
  ```
  databricks auth login --host https://<workspace> --profile <profile>
  ```

## Install

```bash
export PROFILE=my-workspace
export GENIE_SPACE_ID=01f11821c34b1783b8c13e2a0c1b752a
export LAKEBASE_INSTANCE_NAME=genie-cache-db
export DATABASE_NAME=genie_cache_db
export APP_NAME=genie-cache-proxy
export WORKSPACE_USER_EMAIL=me@example.com
export CREATE_INSTANCE=true      # or false to reuse an existing Lakebase instance
export LAKEBASE_CAPACITY=CU_1    # only used when creating

bash skills/genie-cache-install/resources/install.sh
```

The installer:

1. Checks prerequisites + profile validity.
2. Creates (or reuses) the Lakebase instance and waits for it to become `AVAILABLE`.
3. Creates the target database if it doesn't exist.
4. Creates the Databricks App (empty shell) and reads back its service-principal ID.
5. Provisions the `cache.*` schema and grants the app SP the minimum privileges.
6. Renders `app.yaml` from the template, syncs the app code into the workspace.
7. Deploys the app.

At the end you'll see the app URL and a reminder to attach three resources in the app UI (the Lakebase instance, the embedding endpoint, and the Genie space). Redeploy once to pick up the auto-injected env vars — that's it.

## Configure

All knobs live in `app.yaml` (rendered from `app.yaml.template`) and are read at startup. Change them, redeploy.

| Env var | Default | Meaning |
|---|---|---|
| `GENIE_SPACE_ID` | (required) | Space to proxy. |
| `EMBEDDING_ENDPOINT` | `databricks-gte-large-en` | Foundation Model serving endpoint, 1024-dim. |
| `SIMILARITY_THRESHOLD` | `0.80` | Minimum cosine similarity for a semantic-cache hit. |
| `CACHE_TTL_SECONDS` | `86400` | Row TTL (0 = never expire). |
| `CACHE_CLEANUP_INTERVAL_SECONDS` | `3600` | Background cleanup cadence (0 = disabled). |
| `CACHE_MAX_RESULT_ROWS` | `100` | Max result rows inlined per cached response. |

## Verify

```bash
PROFILE=my-workspace APP_NAME=genie-cache-proxy \
  bash skills/genie-cache-install/resources/verify.sh
```

Hits `/health`, `/stats`, and `POST /ask` twice against the deployed app. The second `/ask` should report `source: exact_cache` with a latency under a few hundred ms.

## Layout

```
genie-cache-plugin/
├── .claude-plugin/plugin.json       Plugin manifest
├── README.md                        This file
├── notebooks/                      Embedded path — caching inside a notebook, no app
│   ├── README.md
│   └── genie_cache_embedded.py     Databricks source notebook
└── skills/
    ├── genie-cache-install/         Installer — scaffolds Lakebase + app
    │   ├── SKILL.md
    │   └── resources/
    │       ├── install.sh           Orchestrator
    │       ├── verify.sh            Smoke test
    │       ├── bootstrap.sql        Idempotent schema + grants
    │       └── app_template/        FastAPI app source (synced to workspace)
    ├── genie-cache-stats/           /stats inspector
    │   └── SKILL.md
    └── genie-cache-tune/            SIMILARITY_THRESHOLD tuner
        └── SKILL.md
```

## Two deployment paths

- **App-based (`skills/genie-cache-install/`)** — deploys a FastAPI proxy as a Databricks App. Multiple callers share the cache via HTTP. Use when cache state should outlive any one notebook/job.
- **Embedded (`notebooks/`)** — drops caching helpers directly into a notebook and rewrites `query_genie()` in place. No app, no service principal dance. Use when you just want to cache calls from your own client code.

Both paths target the same Lakebase schema, so you can switch between them without re-provisioning the database.

## License

MIT (see `LICENSE`).
