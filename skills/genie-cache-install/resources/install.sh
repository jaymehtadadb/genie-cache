#!/bin/bash
# Plug-and-play installer for the Genie Cache Proxy.
#
# Required env vars (the skill prompts the user and sets these before calling):
#   PROFILE                         Databricks CLI profile name
#   GENIE_SPACE_ID                  Genie space to cache
#   LAKEBASE_INSTANCE_NAME          Lakebase instance name (new or existing)
#   LAKEBASE_CAPACITY               CU_1 | CU_2 | CU_4 | CU_8  (only if creating)
#   DATABASE_NAME                   Postgres database (e.g., genie_cache_db)
#   APP_NAME                        Databricks App name (e.g., genie-cache-proxy)
#   EMBEDDING_ENDPOINT              Foundation Model endpoint for embeddings
#                                   (default: databricks-gte-large-en, 1024-d)
#   SIMILARITY_THRESHOLD            Cosine similarity cutoff (default: 0.80)
#   CACHE_TTL_SECONDS               Row TTL (default: 86400)
#   CACHE_CLEANUP_INTERVAL_SECONDS  Cleanup loop interval (default: 3600)
#   CACHE_MAX_RESULT_ROWS           Result-row cap per cached response (default: 100)
#   WORKSPACE_USER_EMAIL            The caller's email (for workspace path)
#   CREATE_INSTANCE                 "true" to create, "false" to reuse existing
#
# Usage:
#   PROFILE=my-profile GENIE_SPACE_ID=... LAKEBASE_INSTANCE_NAME=... \
#     DATABASE_NAME=genie_cache_db APP_NAME=genie-cache-proxy \
#     WORKSPACE_USER_EMAIL=me@example.com CREATE_INSTANCE=false \
#     bash install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_TEMPLATE_DIR="$SCRIPT_DIR/app_template"
STAGING_DIR="${STAGING_DIR:-/tmp/genie-cache-staging}"

: "${PROFILE:?PROFILE is required}"
: "${GENIE_SPACE_ID:?GENIE_SPACE_ID is required}"
: "${LAKEBASE_INSTANCE_NAME:?LAKEBASE_INSTANCE_NAME is required}"
: "${DATABASE_NAME:?DATABASE_NAME is required}"
: "${APP_NAME:?APP_NAME is required}"
: "${WORKSPACE_USER_EMAIL:?WORKSPACE_USER_EMAIL is required}"

EMBEDDING_ENDPOINT="${EMBEDDING_ENDPOINT:-databricks-gte-large-en}"
SIMILARITY_THRESHOLD="${SIMILARITY_THRESHOLD:-0.80}"
CACHE_TTL_SECONDS="${CACHE_TTL_SECONDS:-86400}"
CACHE_CLEANUP_INTERVAL_SECONDS="${CACHE_CLEANUP_INTERVAL_SECONDS:-3600}"
CACHE_MAX_RESULT_ROWS="${CACHE_MAX_RESULT_ROWS:-100}"
LAKEBASE_CAPACITY="${LAKEBASE_CAPACITY:-CU_1}"
CREATE_INSTANCE="${CREATE_INSTANCE:-false}"

YQ="${YQ:-/opt/homebrew/bin/yq}"
PSQL="${PSQL:-/opt/homebrew/opt/postgresql@16/bin/psql}"

echo "=== [1/7] Checking prerequisites ==="
databricks --version >/dev/null || { echo "Databricks CLI not installed"; exit 1; }
command -v "$YQ"  >/dev/null || { echo "yq not found at $YQ (override with YQ=)"; exit 1; }
command -v "$PSQL" >/dev/null || { echo "psql not found at $PSQL (override with PSQL=)"; exit 1; }

# Verify profile is valid
if ! databricks auth profiles --output json | "$YQ" -r ".profiles[] | select(.name == \"$PROFILE\") | .valid" | grep -q true; then
  echo "Profile '$PROFILE' is missing or invalid. Run: databricks auth login --profile $PROFILE"
  exit 1
fi

echo "=== [2/7] Resolving Lakebase instance: $LAKEBASE_INSTANCE_NAME ==="
if [ "$CREATE_INSTANCE" = "true" ]; then
  echo "Creating new Lakebase instance (capacity=$LAKEBASE_CAPACITY)..."
  databricks database create-database-instance \
    --json "{\"name\":\"$LAKEBASE_INSTANCE_NAME\",\"capacity\":\"$LAKEBASE_CAPACITY\"}" \
    --profile "$PROFILE" --output json >/dev/null
  # Wait until AVAILABLE
  echo "Waiting for instance to become AVAILABLE (this can take several minutes)..."
  for i in $(seq 1 60); do
    STATE=$(databricks database get-database-instance "$LAKEBASE_INSTANCE_NAME" --profile "$PROFILE" --output json | "$YQ" -r '.state')
    if [ "$STATE" = "AVAILABLE" ]; then
      echo "Instance is AVAILABLE."
      break
    fi
    echo "  state=$STATE (attempt $i/60)"
    sleep 10
  done
  if [ "$STATE" != "AVAILABLE" ]; then
    echo "Timed out waiting for instance to become AVAILABLE."
    exit 1
  fi
fi

# Read instance details
INSTANCE_JSON=$(databricks database get-database-instance "$LAKEBASE_INSTANCE_NAME" --profile "$PROFILE" --output json)
INSTANCE_HOST=$(echo "$INSTANCE_JSON" | "$YQ" -r '.read_write_dns')
INSTANCE_UID=$(echo "$INSTANCE_JSON"  | "$YQ" -r '.uid')
echo "Using instance host: $INSTANCE_HOST"

echo "=== [3/7] Creating database '$DATABASE_NAME' if missing ==="
PG_TOKEN=$(databricks database generate-database-credential \
  --json "{\"instance_names\":[\"$LAKEBASE_INSTANCE_NAME\"],\"request_id\":\"genie-cache-install\"}" \
  --profile "$PROFILE" --output json | "$YQ" -r '.token')

# Check + create DB in the management DB
export PGPASSWORD="$PG_TOKEN"
DB_EXISTS=$("$PSQL" "host=$INSTANCE_HOST port=5432 dbname=databricks_postgres user=$WORKSPACE_USER_EMAIL sslmode=require" -tAc \
  "SELECT 1 FROM pg_database WHERE datname='$DATABASE_NAME'" || true)
if [ "$DB_EXISTS" != "1" ]; then
  "$PSQL" "host=$INSTANCE_HOST port=5432 dbname=databricks_postgres user=$WORKSPACE_USER_EMAIL sslmode=require" \
    -c "CREATE DATABASE \"$DATABASE_NAME\";"
  echo "Created database $DATABASE_NAME."
else
  echo "Database $DATABASE_NAME already exists."
fi

echo "=== [4/7] Creating/updating Databricks App '$APP_NAME' ==="
APP_EXISTS=$(databricks apps get "$APP_NAME" --profile "$PROFILE" --output json 2>/dev/null | "$YQ" -r '.name // ""' || true)
if [ -z "$APP_EXISTS" ]; then
  databricks apps create "$APP_NAME" \
    --description "Semantic caching proxy for Genie space $GENIE_SPACE_ID" \
    --profile "$PROFILE" --output json >/dev/null
  echo "App created. You will need to attach the Lakebase instance, Genie space, and embedding endpoint as resources via the UI before deploy."
else
  echo "App $APP_NAME already exists."
fi

# Fetch the app's service principal ID (we need it for DB grants)
APP_SP=$(databricks apps get "$APP_NAME" --profile "$PROFILE" --output json | "$YQ" -r '.service_principal_client_id // .service_principal_id // ""')
if [ -z "$APP_SP" ]; then
  echo "WARNING: Could not resolve the app's service principal ID automatically."
  echo "After attaching resources in the UI, rerun this step or grant permissions manually."
fi

echo "=== [5/7] Provisioning cache schema in $DATABASE_NAME ==="
if [ -n "$APP_SP" ]; then
  # Replace the :app_sp placeholder in the DDL
  RENDERED_SQL="$STAGING_DIR/bootstrap.sql"
  mkdir -p "$STAGING_DIR"
  /usr/bin/sed "s/:app_sp/$APP_SP/g" "$SCRIPT_DIR/bootstrap.sql" > "$RENDERED_SQL"
  "$PSQL" "host=$INSTANCE_HOST port=5432 dbname=$DATABASE_NAME user=$WORKSPACE_USER_EMAIL sslmode=require" \
    -v ON_ERROR_STOP=1 -f "$RENDERED_SQL"
  echo "Cache schema provisioned and granted to app SP $APP_SP."
else
  # Still create the schema — grants can be added later
  "$PSQL" "host=$INSTANCE_HOST port=5432 dbname=$DATABASE_NAME user=$WORKSPACE_USER_EMAIL sslmode=require" \
    -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector;" \
    -c "CREATE SCHEMA IF NOT EXISTS cache;"
  echo "Schema created; grants skipped (app SP unknown)."
fi

echo "=== [6/7] Rendering app.yaml + staging deployment ==="
mkdir -p "$STAGING_DIR/app"
/bin/cp -R "$APP_TEMPLATE_DIR/." "$STAGING_DIR/app/"
/usr/bin/sed \
  -e "s|__GENIE_SPACE_ID__|$GENIE_SPACE_ID|g" \
  -e "s|__LAKEBASE_INSTANCE_NAME__|$LAKEBASE_INSTANCE_NAME|g" \
  -e "s|__EMBEDDING_ENDPOINT__|$EMBEDDING_ENDPOINT|g" \
  -e "s|__SIMILARITY_THRESHOLD__|$SIMILARITY_THRESHOLD|g" \
  -e "s|__CACHE_TTL_SECONDS__|$CACHE_TTL_SECONDS|g" \
  -e "s|__CACHE_CLEANUP_INTERVAL_SECONDS__|$CACHE_CLEANUP_INTERVAL_SECONDS|g" \
  -e "s|__CACHE_MAX_RESULT_ROWS__|$CACHE_MAX_RESULT_ROWS|g" \
  "$APP_TEMPLATE_DIR/app.yaml.template" > "$STAGING_DIR/app/app.yaml"
/bin/rm -f "$STAGING_DIR/app/app.yaml.template"

WORKSPACE_PATH="/Workspace/Users/$WORKSPACE_USER_EMAIL/$APP_NAME"
databricks sync "$STAGING_DIR/app" "$WORKSPACE_PATH" \
  --exclude __pycache__ --exclude .venv --exclude .DS_Store \
  --full --profile "$PROFILE"

echo "=== [7/7] Deploying app ==="
databricks apps deploy "$APP_NAME" \
  --source-code-path "$WORKSPACE_PATH" \
  --profile "$PROFILE" --output json

APP_URL=$(databricks apps get "$APP_NAME" --profile "$PROFILE" --output json | "$YQ" -r '.url')
echo
echo "Install complete."
echo "  App URL:           $APP_URL"
echo "  Lakebase instance: $LAKEBASE_INSTANCE_NAME ($INSTANCE_HOST)"
echo "  Database:          $DATABASE_NAME"
echo "  Genie space:       $GENIE_SPACE_ID"
echo
echo "Before the app can serve /ask, attach these resources via the app UI:"
echo "  - Database:           $LAKEBASE_INSTANCE_NAME  (Can connect)  → database: $DATABASE_NAME"
echo "  - Model serving:      $EMBEDDING_ENDPOINT       (Can query)"
echo "  - Genie space:        $GENIE_SPACE_ID            (Can run)"
echo
echo "Then redeploy: databricks apps deploy $APP_NAME --source-code-path $WORKSPACE_PATH --profile $PROFILE"
