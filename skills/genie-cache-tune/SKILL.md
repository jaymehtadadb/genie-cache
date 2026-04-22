---
name: genie-cache-tune
description: Tune SIMILARITY_THRESHOLD for the semantic cache. Use when the user reports low hit rate (threshold too high) or wrong answers served from cache (threshold too low).
---

# Tuning SIMILARITY_THRESHOLD

`SIMILARITY_THRESHOLD` is the cosine-similarity floor for a semantic-cache hit. Default `0.80`.

- **Too high** (e.g., `0.90`): near-paraphrases miss — "revenue last quarter" vs. "Q1 revenue" don't match. Hit rate drops.
- **Too low** (e.g., `0.65`): distinct questions get the same cached answer — "top 5 customers" and "bottom 5 customers" both match. Wrong answers.

This skill helps find a threshold that balances the two by sampling pairs from the current cache and showing how they rank.

## Prerequisites

The proxy must already have meaningful traffic — at least a few dozen questions in `cache.semantic_cache`. Tuning against an empty cache tells you nothing.

## Inputs

- `PROFILE` — CLI profile.
- `LAKEBASE_INSTANCE_NAME` — The Lakebase instance hosting the cache.
- `DATABASE_NAME` — Cache database.
- `WORKSPACE_USER_EMAIL` — For Postgres connect.

## Steps

### 1. Connect to the cache database

```
TOKEN=$(databricks database generate-database-credential \
  --json '{"instance_names":["<LAKEBASE_INSTANCE_NAME>"],"request_id":"tune"}' \
  --profile <PROFILE> --output json | yq -r '.token')
HOST=$(databricks database get-database-instance <LAKEBASE_INSTANCE_NAME> \
  --profile <PROFILE> --output json | yq -r '.read_write_dns')
export PGPASSWORD=$TOKEN
psql "host=$HOST port=5432 dbname=<DATABASE_NAME> user=<email> sslmode=require"
```

### 2. Find borderline pairs

Pairs with similarity near the current threshold are the ones to inspect — they're the decisions most sensitive to change.

```sql
WITH pairs AS (
  SELECT
    a.question AS q1,
    b.question AS q2,
    1 - (a.embedding <=> b.embedding) AS sim
  FROM cache.semantic_cache a
  JOIN cache.semantic_cache b ON a.id < b.id
)
SELECT q1, q2, ROUND(sim::numeric, 3) AS similarity
FROM pairs
WHERE sim BETWEEN 0.70 AND 0.90
ORDER BY sim DESC
LIMIT 30;
```

Read the 30 rows. For each:

- If `q1` and `q2` *should* share an answer — threshold must be **below** `sim`.
- If they should *not* share an answer — threshold must be **above** `sim`.

The right threshold is the gap between the highest "different intent" similarity and the lowest "same intent" similarity.

### 3. Apply the new threshold

Edit `app.yaml` on the deployed app and set `SIMILARITY_THRESHOLD` to the new value, then redeploy:

```
databricks apps deploy <APP_NAME> \
  --source-code-path /Workspace/Users/<email>/<APP_NAME> \
  --profile <PROFILE>
```

The new threshold takes effect immediately on the next `/ask` request.

## Reference points

- `0.90+` — only near-identical wording matches. Use when the underlying data is volatile and false-positive hits would be harmful.
- `0.80` — default. Works well for most dashboards.
- `0.70` — aggressive. Only use if the space has a narrow set of distinct questions.
- `< 0.65` — almost certainly too low; you'll serve wrong answers.
