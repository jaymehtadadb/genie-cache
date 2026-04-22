#!/bin/bash
# Smoke-test a deployed genie-cache proxy.
#   PROFILE=... APP_NAME=... bash verify.sh

set -euo pipefail
: "${PROFILE:?PROFILE is required}"
: "${APP_NAME:?APP_NAME is required}"

YQ="${YQ:-/opt/homebrew/bin/yq}"

TOKEN=$(databricks auth token --profile "$PROFILE" | "$YQ" -r '.access_token')
APP_URL=$(databricks apps get "$APP_NAME" --profile "$PROFILE" --output json | "$YQ" -r '.url')

echo "App URL: $APP_URL"
echo
echo "=== GET /health ==="
curl -sS -H "Authorization: Bearer $TOKEN" "$APP_URL/health"
echo
echo
echo "=== GET /stats ==="
curl -sS -H "Authorization: Bearer $TOKEN" "$APP_URL/stats"
echo
echo
echo "=== POST /ask (cache miss expected) ==="
Q="${VERIFY_QUESTION:-Return one example row for testing.}"
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"question\":\"$Q\"}" "$APP_URL/ask" | "$YQ" '{source, latency_ms}'
echo
echo "=== POST /ask (exact-cache hit expected) ==="
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"question\":\"$Q\"}" "$APP_URL/ask" | "$YQ" '{source, latency_ms}'
