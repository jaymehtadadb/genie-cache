-- Idempotent cache schema bootstrap.
-- Run as a Postgres user that has CREATE on the database.
-- The application service principal receives the minimum grants at the bottom.

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

-- HNSW approximate-nearest-neighbor index on cosine operator class.
CREATE INDEX IF NOT EXISTS semantic_cache_embedding_hnsw_idx
    ON cache.semantic_cache
    USING hnsw (embedding vector_cosine_ops);

-- Partial indexes keep expired-row cleanup cheap even at millions of rows.
CREATE INDEX IF NOT EXISTS exact_cache_expires_idx
    ON cache.exact_cache (expires_at)
    WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS semantic_cache_expires_idx
    ON cache.semantic_cache (expires_at)
    WHERE expires_at IS NOT NULL;

-- Grant the app service principal access. The installer substitutes :app_sp.
GRANT USAGE ON SCHEMA cache TO ":app_sp";
GRANT SELECT, INSERT, UPDATE, DELETE ON cache.exact_cache TO ":app_sp";
GRANT SELECT, INSERT, UPDATE, DELETE ON cache.semantic_cache TO ":app_sp";
GRANT USAGE, SELECT ON SEQUENCE cache.semantic_cache_id_seq TO ":app_sp";
