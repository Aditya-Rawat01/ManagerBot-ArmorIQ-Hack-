-- 0001_extensions.sql — required PostgreSQL extensions (P2-01).
--
-- Runs as the migration role (Neon project owner / local compose owner).
-- The `vector` extension powers knowledge_chunks.embedding (384 dims,
-- FastEmbed BAAI/bge-small-en-v1.5, PRD v2 §8.2). Full-text search uses the
-- built-in tsvector/GIN support, so no other extension is required.

CREATE EXTENSION IF NOT EXISTS vector;
