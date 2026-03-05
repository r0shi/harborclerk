-- Runs on first postgres container init (mounted into /docker-entrypoint-initdb.d/)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS citext;
