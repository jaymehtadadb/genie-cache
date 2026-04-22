---
name: genie-cache-stats
description: Inspect a deployed Genie cache proxy. Use when the user wants to see cache hit rates, row counts, TTL settings, or the number of expired rows waiting for cleanup.
---

# Inspecting Cache Stats

Fetches and formats the `/stats` endpoint from a deployed genie-cache proxy.

## Inputs

- `PROFILE` — Databricks CLI profile.
- `APP_NAME` — Deployed app name (e.g., `genie-cache-proxy`).

## Steps

1. Get the app URL:
   ```
   databricks apps get <APP_NAME> --profile <PROFILE> --output json
   ```
   Extract `.url` with `yq -r '.url'`.

2. Get an auth token:
   ```
   databricks auth token --profile <PROFILE>
   ```
   Extract `.access_token`.

3. Hit `/stats`:
   ```
   curl -sS -H "Authorization: Bearer <token>" <app_url>/stats
   ```

## What the response means

```json
{
  "exact_cache": {
    "total_rows": 42,
    "expired_rows": 3,
    "total_hits": 187,
    "oldest_entry": "2026-04-18T14:02:11Z",
    "newest_entry": "2026-04-21T09:47:02Z"
  },
  "semantic_cache": { ... },
  "ttl_seconds": 86400,
  "max_result_rows": 100
}
```

- `total_rows` — current row count (including expired but not yet cleaned).
- `expired_rows` — rows whose `expires_at <= NOW()`. The background cleanup task will delete these on its next tick.
- `total_hits` — sum of `hit_count` across rows. A rough proxy for traffic savings — each hit is one Genie call avoided.
- `ttl_seconds` and `max_result_rows` — the live values the app is using. If these don't match what you think you configured in `app.yaml`, the app hasn't been redeployed.

## Force a cleanup cycle

If `expired_rows` is high and you don't want to wait for the next cleanup tick:

```
curl -sS -X POST -H "Authorization: Bearer <token>" <app_url>/admin/cleanup
```

Returns `{exact_deleted, semantic_deleted, ttl_seconds}`.
