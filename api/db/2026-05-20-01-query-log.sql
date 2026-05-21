-- Migration: Add query_log table for search performance telemetry
-- Records one row per /search request (cache misses only for DB timings; cache hits for frequency analysis).
-- Used to identify slow query patterns and map URL ?q= parameters back to DB execution times.

CREATE TABLE IF NOT EXISTS magic.query_log (
    id            BIGSERIAL PRIMARY KEY,
    logged_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    q             TEXT,
    orderby       TEXT,       -- ?orderby= URL parameter
    unique_by     TEXT,       -- ?unique= URL parameter ("unique" is reserved in SQL)
    cache_hit     BOOLEAN NOT NULL DEFAULT false,
    execute_ms    REAL,       -- inner_timings["execute_query"]; NULL on cache hits
    fetch_ms      REAL,       -- inner_timings["fetch_results"]; NULL on cache hits
    total_ms      REAL,       -- wall-clock time from request start to middleware response
    result_count  INTEGER,    -- number of cards returned in this response
    total_cards   INTEGER,    -- total matching cards (before LIMIT)
    had_error     BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_query_log_logged_at
    ON magic.query_log (logged_at DESC);

CREATE INDEX IF NOT EXISTS idx_query_log_execute_ms
    ON magic.query_log (execute_ms DESC NULLS LAST);

-- Enable pg_stat_statements (already in shared_preload_libraries in postgresql.conf).
-- This is idempotent; safe to run if the extension is already present.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
