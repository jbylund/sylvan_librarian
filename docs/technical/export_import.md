# Card Data Export/Import (removed)

The `GET /export_card_data` and `GET /import_card_data` routes this page used to document — a
timestamped JSON dump of `magic.cards`, `magic.tags` and `magic.tag_relationships` under
`/data/api/exports/`, and a truncate-and-reload from one — were removed in #528 ("Replace GraphQL
Tag Import with Scryfall Bulk Data"), together with their handlers and tests. There is no JSON
export/import of the database any more, and the tag tables were renamed to `magic.oracle_tags` and
`magic.oracle_tag_relationships` by `api/db/2026-06-21-01-bulk-tag-import.sql`.

What does the job today:

- **Card data** is (re)built from Scryfall bulk data by `import_data` in `api/admin_resource.py`,
  mounted under the `_admin` prefix and also called once at API startup, so a fresh database
  populates itself.
- **Tags** are loaded from Scryfall bulk data by the `import_oracle_tags`, `import_art_tags` and
  `import_all_is_tags` admin handlers in the same file.
- **Backups and moving a database between hosts** are done with Postgres's own tooling against the
  stack's container, which exposes no host port — for example:

  ```bash
  docker compose --project-name sylvan_blue --env-file .env --env-file .env.generated \
    --env-file envs/blue --file docker-compose.yml exec -T postgres \
    pg_dump -U "$(jq -r .XPGUSER env.json)" -d "$(jq -r .XPGDATABASE env.json)" \
    --format=custom > sylvan_blue.dump
  ```

  (The credentials live in `env.json`; `make dbconn-blue` runs the same
  `docker compose ... exec postgres` invocation for psql.)
