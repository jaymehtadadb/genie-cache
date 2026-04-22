---
name: genie-cache-install
description: Install the Genie semantic-cache proxy. Use when the user wants to put a caching layer in front of a Databricks Genie space — creates or reuses a Lakebase Postgres instance, provisions the cache schema, and deploys a FastAPI proxy as a Databricks App.
---

# Installing the Genie Cache Proxy

This skill walks the user through deploying a caching proxy for a Databricks Genie space. The end state: a Databricks App URL where `POST /ask` checks Lakebase first and only falls through to Genie on a cache miss.

## Prerequisites to confirm before starting

Verify each of these before calling the installer. If anything is missing, stop and tell the user what to fix.

1. `databricks --version` ≥ 0.229.0.
2. `yq` is installed (`command -v yq`). Default path: `/opt/homebrew/bin/yq`.
3. `psql` is installed (`command -v /opt/homebrew/opt/postgresql@16/bin/psql`). On Linux, override via `PSQL=` env var.
4. The workspace profile is valid:
   ```
   databricks auth profiles --output json | yq -r '.profiles[] | select(.name == "<PROFILE>") | .valid'
   ```
   Must print `true`. If not, ask the user to run `databricks auth login --host <workspace-url> --profile <PROFILE>`.

## Inputs to collect from the user

Ask once, up front. Don't re-prompt for anything you can infer.

| Input | Notes |
|---|---|
| `PROFILE` | CLI profile for the target workspace. |
| `GENIE_SPACE_ID` | The Genie space to cache. Find it in the space URL: `/genie/rooms/<id>`. |
| `CREATE_INSTANCE` | `true` to create a new Lakebase instance, `false` to reuse one. |
| `LAKEBASE_INSTANCE_NAME` | Either the new instance name, or the existing one to reuse. |
| `LAKEBASE_CAPACITY` | Only when creating. One of `CU_1`, `CU_2`, `CU_4`, `CU_8`. Default `CU_1`. |
| `DATABASE_NAME` | Postgres database name to create/use. Suggest `genie_cache_db`. |
| `APP_NAME` | Databricks App name. Suggest `genie-cache-proxy`. |
| `WORKSPACE_USER_EMAIL` | The caller's workspace email (used as Postgres superuser during install). |
| `EMBEDDING_ENDPOINT` | Default `databricks-gte-large-en` (1024-dim). Only override if the user has a different endpoint AND updates `VECTOR(1024)` in `bootstrap.sql` to match. |
| `SIMILARITY_THRESHOLD` | Default `0.80`. Raise for precision, lower for recall. |
| `CACHE_TTL_SECONDS` | Default `86400` (24 h). Set `0` for no expiry. |
| `CACHE_CLEANUP_INTERVAL_SECONDS` | Default `3600`. Set `0` to disable. |
| `CACHE_MAX_RESULT_ROWS` | Default `100`. Cap on rows stored per cached response. |

## Run the installer

Export the inputs and run `resources/install.sh`. Prefer writing a wrapper script to a temp file and invoking it via `bash /tmp/run_install.sh` rather than passing long inline env expansions. Example wrapper:

```bash
#!/bin/bash
set -euo pipefail
export PROFILE=anil-workspace
export GENIE_SPACE_ID=01f11821c34b1783b8c13e2a0c1b752a
export LAKEBASE_INSTANCE_NAME=genie-loadtest-db
export DATABASE_NAME=genie_cache_db
export APP_NAME=genie-cache-proxy
export WORKSPACE_USER_EMAIL=me@example.com
export CREATE_INSTANCE=false
export EMBEDDING_ENDPOINT=databricks-gte-large-en
export SIMILARITY_THRESHOLD=0.80
export CACHE_TTL_SECONDS=86400
export CACHE_CLEANUP_INTERVAL_SECONDS=3600
export CACHE_MAX_RESULT_ROWS=100
bash <PLUGIN_DIR>/skills/genie-cache-install/resources/install.sh
```

The installer prints progress through seven numbered steps. Expected total runtime: 2–5 minutes (reuse) or 5–15 minutes (new instance).

## Post-install steps (the UI bit the installer can't automate)

After the installer finishes, three resources must be attached via the Databricks App UI before `/ask` will serve traffic. Tell the user to:

1. Go to **Compute → Apps → `<APP_NAME>` → Edit → Resources**.
2. Add:
   - **Database** → `<LAKEBASE_INSTANCE_NAME>` → permission `Can connect` → database `<DATABASE_NAME>`.
   - **Model serving endpoint** → `<EMBEDDING_ENDPOINT>` → permission `Can query`.
   - **Genie space** → `<GENIE_SPACE_ID>` → permission `Can run`.
3. Redeploy:
   ```
   databricks apps deploy <APP_NAME> \
     --source-code-path /Workspace/Users/<email>/<APP_NAME> \
     --profile <PROFILE>
   ```

The redeploy picks up the auto-injected `PGHOST`, `DATABASE_NAME`, `SERVING_ENDPOINT_NAME`, and `GENIE_SPACE_ID` env vars from the attached resources.

## Verify

Run the smoke test:

```bash
PROFILE=<PROFILE> APP_NAME=<APP_NAME> \
  bash <PLUGIN_DIR>/skills/genie-cache-install/resources/verify.sh
```

First `/ask` should report `source: genie` with a latency in the 5–30 s range. Second `/ask` with the same question should report `source: exact_cache` with latency under 500 ms. If it still reports `genie`, the app SP grants are missing — re-run the bootstrap step or check logs at `<app_url>/logz`.

## Troubleshooting

- **`column "expires_at" does not exist`** in the app logs → the service principal lacks `ALTER TABLE` on `cache.*`. Re-run `bootstrap.sql` as a Postgres superuser (the user who owns the tables), substituting the app SP ID for `:app_sp`.
- **`/ask` always reports `source: genie`** → attach the embedding endpoint + Lakebase resources in the app UI, then redeploy.
- **`GET /stats` returns 500** → Lakebase instance not yet `AVAILABLE`, or app can't reach it. Check `databricks database get-database-instance <name> --profile <PROFILE>`.
- **Low cache hit rate** → use the `genie-cache-tune` skill to find a better `SIMILARITY_THRESHOLD`.
