# Setup

Two deployment paths. Pick one based on who needs the cache. Both target the same Lakebase schema, so switching later is free.

| If you want… | Use |
|---|---|
| Multiple callers (notebooks, jobs, apps) sharing one cache over HTTP | App-based — see [App path](#app-path) |
| Caching inside one notebook or one client, no service-principal setup | Embedded — see [Embedded path](#embedded-path) |

## Prerequisites (both paths)

- **Databricks workspace** with Apps + Lakebase enabled (FE-VM serverless, or any workspace with Lakebase).
- **Genie space** already built. Grab its space ID from the Genie UI URL.
- **Local tooling**:
  - `databricks` CLI 0.229.0+ — `brew upgrade databricks`
  - `yq` — `brew install yq`
  - `psql` from `postgresql@16` — `brew install postgresql@16`
- **Authenticated CLI profile**:
  ```bash
  databricks auth login --host https://<workspace> --profile <profile>
  ```

## App path

Deploys a FastAPI proxy as a Databricks App. Use when cache state should outlive any one notebook or job.

```bash
export PROFILE=my-workspace
export GENIE_SPACE_ID=01f11821c34b1783b8c13e2a0c1b752a
export LAKEBASE_INSTANCE_NAME=genie-cache-db
export DATABASE_NAME=genie_cache_db
export APP_NAME=genie-cache-proxy
export WORKSPACE_USER_EMAIL=me@example.com
export CREATE_INSTANCE=true      # false to reuse an existing Lakebase instance
export LAKEBASE_CAPACITY=CU_1    # only used when creating

bash skills/genie-cache-install/resources/install.sh
```

The installer:

1. **Checks prerequisites** + profile validity.
2. **Creates (or reuses) the Lakebase instance** and waits for `AVAILABLE`.
3. **Creates the database** inside the instance if it doesn't exist.
4. **Creates the Databricks App** (empty shell) and reads back its service-principal ID.
5. **Provisions the `cache.*` schema** and grants the app SP the minimum privileges.
6. **Renders `app.yaml`** from template, syncs app code into the workspace.
7. **Deploys the app.**

The installer prints the app URL and a reminder to attach three resources in the app UI — Lakebase instance, embedding endpoint, Genie space — then redeploy once to pick up the auto-injected env vars.

### Verify

```bash
PROFILE=my-workspace APP_NAME=genie-cache-proxy \
  bash skills/genie-cache-install/resources/verify.sh
```

Hits `/health`, `/stats`, and two sequential `POST /ask` calls. The second should report `source: exact_cache` with sub-100 ms latency.

### Configure

All knobs live in `app.yaml` (rendered from `app.yaml.template`) and are read at startup. Change them, redeploy.

| Env var | Default | Meaning |
|---|---|---|
| `GENIE_SPACE_ID` | (required) | Space to proxy. |
| `EMBEDDING_ENDPOINT` | `databricks-gte-large-en` | 1024-dim foundation-model endpoint. |
| `SIMILARITY_THRESHOLD` | `0.80` | Min cosine similarity for a semantic-cache hit. |
| `CACHE_TTL_SECONDS` | `86400` | Row TTL (0 = never expire). |
| `CACHE_CLEANUP_INTERVAL_SECONDS` | `3600` | Background cleanup cadence (0 = disabled). |
| `CACHE_MAX_RESULT_ROWS` | `100` | Max result rows inlined per cached response. |

## Embedded path

Drops caching helpers directly into a notebook and rewrites `query_genie()` in place. No app, no service-principal dance.

```bash
databricks workspace import \
  /Users/you@company.com/genie_cache_embedded \
  --file notebooks/genie_cache_embedded.py \
  --language PYTHON \
  --format SOURCE \
  -p my-workspace
```

Then open the notebook in Databricks and run top-to-bottom. The notebook is idempotent:

1. `%pip install --upgrade "psycopg[binary,pool]>=3.2.0" pgvector "databricks-sdk>=0.40.0"`
2. Config cell — fill in `GENIE_SPACE_ID`, `LAKEBASE_INSTANCE_NAME`, `CREATE_INSTANCE_IF_MISSING`.
3. Instance + database bootstrap (creates if missing, waits for `AVAILABLE`).
4. Schema bootstrap (`CREATE EXTENSION vector`, `cache.exact_cache`, `cache.semantic_cache`, HNSW index, partial TTL indexes).
5. **Customer's original `query_genie` verbatim** — as a reference point.
6. Caching helpers — pool with `max_lifetime=2700`, `_hash`, `_embed`, `_check_exact`, `_check_semantic`, `_write_cache`, `cleanup_expired`.
7. **Drop-in `query_genie` replacement** with exact → semantic → SDK fallback flow.
8. Demo + inspect + optional cleanup cells.

See [`notebooks/README.md`](../notebooks/README.md) for a longer walkthrough and the full knob list.

## Switching between paths

Because both paths write to the same `cache.exact_cache` / `cache.semantic_cache` tables in the same Lakebase database, you can:

- Start embedded in a notebook, then stand up the app later — existing cache entries are immediately reusable.
- Run both concurrently (notebook + app) against the same database — the schema tolerates it. Just beware that the `genie_space_id` column is the only tenancy boundary.

## Tearing down

```bash
# Drop the app
databricks apps delete $APP_NAME -p $PROFILE

# Drop the schema (keeps the database + instance intact)
psql "..." -c "DROP SCHEMA cache CASCADE;"

# Drop the database
psql "host=... dbname=databricks_postgres ..." -c "DROP DATABASE $DATABASE_NAME;"

# Drop the Lakebase instance (irreversible!)
databricks database delete-database-instance $LAKEBASE_INSTANCE_NAME -p $PROFILE
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `AttributeError: 'WorkspaceClient' object has no attribute 'database'` | SDK too old. `%pip install --upgrade "databricks-sdk>=0.40.0"` then restart Python. |
| `database "..." does not exist` | You pointed `PG_DATABASE` at the instance name, not the database. They're different — the instance is the Postgres server; the database lives inside it. |
| `TypeError: 'EmbeddingsV1ResponseEmbeddingElement' object is not subscriptable` | Use `.embedding` attribute, not `["embedding"]`. Fixed in the notebook; check your local copy is current. |
| `cannot dump lists of mixed types; got: float, int` | Embedding endpoint returns ints for exact 0s. Cast with `[float(x) for x in raw]` before passing to pgvector. |
| `verify.sh` second `/ask` returns `source: genie` instead of `exact_cache` | The app SP likely lacks `UPDATE` on `cache.exact_cache`. Re-run `bootstrap.sql` with the correct `:app_sp` substitution. |
