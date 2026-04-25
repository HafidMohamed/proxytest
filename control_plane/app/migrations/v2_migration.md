# v2 Database Migration

Run these SQL statements when upgrading from v1 to v2.
Or just restart the app — `Base.metadata.create_all()` auto-creates new tables.
The `ALTER TABLE` statements are needed for existing databases.

```sql
-- NEW TABLES (auto-created on first startup)
-- translation_memory, glossary_rules, usage_events

-- COLUMN ADDITIONS (run manually on existing DBs)
ALTER TABLE customers ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(128);
ALTER TABLE customers ADD COLUMN IF NOT EXISTS api_key_prefix VARCHAR(12);
ALTER TABLE customers ADD COLUMN IF NOT EXISTS plan VARCHAR(32) DEFAULT 'free';
ALTER TABLE customers ADD COLUMN IF NOT EXISTS monthly_word_limit INTEGER DEFAULT 2000;
ALTER TABLE domains ADD COLUMN IF NOT EXISTS routing_mode VARCHAR(32) DEFAULT 'subdirectory';
ALTER TABLE translation_configs ADD COLUMN IF NOT EXISTS routing_mode VARCHAR(32) DEFAULT 'subdirectory';
ALTER TABLE translated_pages ADD COLUMN IF NOT EXISTS html_url VARCHAR(2048);
ALTER TABLE translated_pages ADD COLUMN IF NOT EXISTS word_count INTEGER DEFAULT 0;

-- IMPORTANT: api_key column removed from customers.
-- If upgrading from v1, migrate existing keys first:
-- UPDATE customers SET api_key_hash = encode(sha256(api_key::bytea), 'hex'),
--                     api_key_prefix = 'sk-' || substring(api_key, 1, 8)
-- WHERE api_key_hash IS NULL;
```
