# pyxle-db

`pyxle-db` is Pyxle's official database plugin over **SQLite, PostgreSQL, and MySQL**. It offers two first-class paths: an **explicit-SQL** API (portable placeholders, a uniform `Row` type, checksum-tracked migrations — you write SQL, it makes that SQL portable) and an optional **SQLAlchemy ORM** path. Either way, every loader and action gets a request-scoped database handle, and writes commit or roll back automatically.

> **Version 0.3.0.** SQLite needs nothing beyond the package; PostgreSQL, MySQL, and the SQLAlchemy ORM ship as extras (the base install stays SQLAlchemy-free). Every backend-specific behaviour documented here is enforced by test suites that run against real PostgreSQL 16 and MySQL 8 servers in CI.

## Install

```bash
pip install pyxle-db                # SQLite (stdlib driver, zero extra deps)
pip install "pyxle-db[postgres]"    # + asyncpg
pip install "pyxle-db[mysql]"       # + asyncmy and cryptography
```

The `mysql` extra includes `cryptography` because MySQL 8's default
`caching_sha2_password` authentication requires it at connect time.

## Quickstart

Add the plugin to `pyxle.config.json` (see the [plugins guide](../guides/plugins.md) for how plugin loading works):

```json
{
  "plugins": [
    {
      "name": "pyxle-db",
      "settings": { "path": "data/app.db", "migrationsDir": "migrations" }
    }
  ]
}
```

At startup the plugin opens the database, applies any pending migrations, and registers the shared `Database` instance. Loaders and actions reach it through `get_database()`:

```python
from pyxle.runtime import server
from pyxle_db import get_database


@server
async def load(request):
    db = get_database()
    rows = await db.fetchall(
        "SELECT id, title FROM posts WHERE published = ? ORDER BY id DESC",
        (True,),
    )
    return {"posts": [row.asdict() for row in rows]}
```

## Plugin settings

All settings are optional — with none, you get SQLite at `./data/app.db`.

| Key | Default | What it does |
|---|---|---|
| `path` | `"./data/app.db"` | SQLite file path, relative to the project root. |
| `url` | — | Full database URL (takes precedence over `path`). Supports `env:` indirection. |
| `migrationsDir` | `"migrations"` | Directory of migration files, applied at startup. |
| `waitForFileMs` | `0` | Milliseconds to wait for a SQLite file to appear before failing (useful when another process creates it). |

**Never commit credentials.** The `url` setting supports `env:` indirection — the committed config names an environment variable, the deploy environment supplies the secret:

```json
{
  "plugins": [
    { "name": "pyxle-db", "settings": { "url": "env:DATABASE_URL" } }
  ]
}
```

Startup fails with a clear error if the named variable is unset, rather than silently falling back to SQLite.

The plugin registers three services: `db.database` (the `Database`), `db.url` (the connection URL with credentials redacted), and — for SQLite — `db.path` (the resolved file path).

## Database URLs

```
sqlite:///relative/path.db        postgresql://user:pass@host:5432/dbname
sqlite:////absolute/path.db       mysql://user:pass@host:3306/dbname
```

A bare filesystem path is also accepted and treated as SQLite. Server backends accept pool sizing as URL options:

```
postgresql://app:secret@db.internal:5432/appdb?pool_min=2&pool_max=10
```

Other PostgreSQL options (such as `application_name`) pass through to asyncpg as server settings. The MySQL backend pins every pooled session to UTC (`SET time_zone = '+00:00'`), so `TIMESTAMP` columns and `NOW()` are never shifted through the server's system time zone.

## Queries and rows

Four query methods, identical on every backend:

```python
count = await db.execute("UPDATE posts SET views = views + 1 WHERE id = ?", (7,))
row   = await db.fetchone("SELECT * FROM posts WHERE id = ?", (7,))   # Row | None
rows  = await db.fetchall("SELECT * FROM posts ORDER BY id")          # list[Row]
row   = await db.get("SELECT * FROM posts WHERE id = ?", (7,))        # raises NotFoundError
```

`execute` returns the affected row count. Every read returns the same `Row` type — immutable, accessible by index **and** by column name:

```python
row[0]            # by position
row["title"]      # by name
row.get("slug")   # with a default, dict-style
row.asdict()      # plain dict — feeds anything that takes mappings
```

`Row.asdict()` is the bridge to whatever model layer you prefer; no integration code needed:

```python
post = Post(**row.asdict())                    # stdlib dataclass
post = PostModel.model_validate(row.asdict())  # pydantic v2
```

## Placeholders

Always write `?`. pyxle-db rewrites it to each backend's native style — `?` for SQLite, `$1`/`$2` for PostgreSQL, `%s` for MySQL — so the SQL in your codebase never forks per engine.

When you need a *literal* question mark (PostgreSQL's JSON operators), escape it as `??`:

```python
await db.fetchall(
    "SELECT id FROM events WHERE payload ?? 'user_id' AND kind = ?",
    ("signup",),
)
```

The rewriter is literal-aware: `?` inside string literals, quoted identifiers, comments, and dollar-quoted bodies is never touched, so data can't become SQL structure during translation.

## Transactions

```python
async with db.transaction() as tx:
    await tx.execute("INSERT INTO orders (id, total) VALUES (?, ?)", (oid, total))
    await tx.execute("UPDATE stock SET qty = qty - 1 WHERE sku = ?", (sku,))
# commits on clean exit, rolls back if the block raises
```

The transaction object carries the full query surface (`execute`, `executemany`, `fetchone`, `fetchall`, `get`). Two escape hatches are SQLite-only and raise `UnsupportedOperationError` on server backends: `db.sync_transaction()` and `db.close()` — prefer `await db.aclose()`, which works everywhere.

## Request-scoped access and auto-transactions

With the plugin installed, every loader and action gets a lazy database handle on `request.state.db` — no import, no service lookup. A request that never queries opens no connection.

```python
@server
async def load(request):
    rows = await request.state.db.fetchall("SELECT * FROM posts ORDER BY id DESC")
    return {"posts": rows}

@action
async def create_post(request):
    await request.state.db.execute("INSERT INTO posts (title) VALUES (?)", (title,))
    return {"ok": True}   # committed automatically on success
```

On an unsafe method (`POST`/`PUT`/`PATCH`/`DELETE`) the request's writes run inside **one transaction that commits when the action succeeds and rolls back when it fails** — where "fails" means the action raised `ActionError` (or any exception), which Pyxle turns into a non-2xx response. You never call `commit()`/`rollback()`, and a failed action never leaves a partial write behind. `GET`/`HEAD` run read-only.

Opt out per action with `@no_auto_transaction` (then manage `async with request.state.db.transaction()` yourself), or app-wide with `"autoTransactions": false`.

## The ORM path (SQLAlchemy)

Prefer an ORM? Install `pip install 'pyxle-db[sqlalchemy]'` and set `"orm": {"metadata": "app.models:Base"}` in the plugin settings. Models subclass `pyxle_db.orm.Base`; loaders and actions get a request-scoped `AsyncSession` on `request.state.session` under the same auto-transaction rules:

```python
from sqlalchemy import select

@server
async def load(request):
    notes = (await request.state.session.scalars(select(Note))).all()
    return {"notes": [n.body for n in notes]}
```

SQLAlchemy errors surface as the **same** pyxle-db error types as the explicit-SQL path. The base install stays SQLAlchemy-free.

## The `pyxle-db` CLI

```bash
pyxle-db migrate              # apply pending checksum migrations
pyxle-db status               # applied vs pending
pyxle-db alembic-init         # scaffold Alembic for the ORM path
pyxle-db revision -m "…" --autogenerate
pyxle-db upgrade head         # / downgrade / current / history
```

The CLI reads the same `pyxle.config.json` + `.env` the app uses. Pick one migration tool per app: the checksum migrator for explicit-SQL, Alembic for the ORM.

## Datetimes

One contract on all three engines, in both directions:

- **Reads** always return timezone-aware UTC `datetime` objects — including from naive `TIMESTAMP` columns, which are interpreted as UTC.
- **Binds** accept either naive datetimes (assumed UTC) or aware ones (converted to UTC before binding).

Without this, the engines disagree sharply: asyncpg rejects aware datetimes for `TIMESTAMP` columns outright, and MySQL's driver would silently serialise an aware datetime's foreign wall clock. pyxle-db normalises at the backend boundary so application code never thinks about it.

## Migrations

Migrations are plain SQL files in the configured directory, named `<NNN>-<slug>.sql`:

```
migrations/
├── 0001-initial-schema.sql
├── 0002-add-tags.sql
└── 0002-add-tags.mysql.sql      ← per-dialect override
```

Rules the migrator enforces:

- **Applied exactly once, atomically.** Each migration's statements and its tracking-table insert commit together or not at all.
- **Checksum-tracked.** Editing an already-applied migration is detected and rejected — write a new migration instead.
- **Per-dialect overrides.** `<NNN>-<slug>.<dialect>.sql` replaces the base file on that backend (the example above uses MySQL-specific DDL while SQLite and PostgreSQL share the base file). An override with no base file is a backend-only migration.
- **Namespaced tracking.** The default tracking table is `schema_migrations`; libraries that bring their own migrations onto your database (pyxle-auth does) use a private tracking table via `Migrator(db, dir, tracking_table=...)` so two migration histories never see each other as drift.

The plugin applies pending migrations at startup. You can also drive the `Migrator` directly:

```python
from pyxle_db import Migrator

applied = await Migrator(db, Path("migrations")).apply_all()
```

## Writing portable schemas

These rules are proven against real PostgreSQL and MySQL servers — the live conformance suites enforce them:

- **`VARCHAR(n)` for every key or indexed column.** MySQL cannot index bare `TEXT` (error 1170). SQLite and PostgreSQL treat `VARCHAR` exactly like `TEXT`, so nothing is lost. Keep `TEXT` for payloads.
- **`TIMESTAMP` columns are fine on SQLite and PostgreSQL.** On MySQL prefer `DATETIME(6)` via a per-dialect override: MySQL's `TIMESTAMP` is capped at 2038 and rounds to whole seconds.
- **MySQL has no `CREATE INDEX IF NOT EXISTS`.** Create indexes in migrations (they run exactly once) or probe `information_schema.statistics` first.
- **Spell out inserted values instead of relying on column `DEFAULT`s**, which drift subtly between engines.

## Errors

One hierarchy regardless of driver — application code never imports `sqlite3`, `asyncpg`, or `asyncmy`:

| Exception | Raised when |
|---|---|
| `DatabaseError` | Base class for everything below. |
| `IntegrityError` | Constraint violation (unique, foreign key, …). |
| `OperationalError` | Connection refused, pool exhaustion, server gone. |
| `ConfigurationError` | Bad URL, bad pool option, unset `env:` variable. |
| `UnsupportedOperationError` | SQLite-only call on a server backend. |
| `NotFoundError` | `db.get(...)` found no row. |
| `MigrationError` / `MigrationChecksumMismatch` | Malformed or edited migrations. |

## The DatabaseLike contract

Plugins that need a database (like [pyxle-auth](pyxle-auth.md)) don't depend on the `Database` class — they bind to the `pyxle_db.DatabaseLike` protocol: `execute`, `fetchone`, `fetchall`, an async-context-manager `transaction()`, and a `dialect` property. Any object satisfying it, registered as the `db.database` service, can stand in — an adapter over another engine, a test fake. `Database` is the reference implementation, and the protocol is `runtime_checkable`:

```python
from pyxle_db import DatabaseLike

assert isinstance(my_adapter, DatabaseLike)
```

The full replacement contract (error translation, dialect names, datetime semantics) is documented in the protocol's docstring and exercised by pyxle-auth's contract test suite.

## Using it without the plugin

Scripts, tests, and workers can open a database directly:

```python
from pyxle_db import Database, connect

db = await connect("data/app.db")                      # SQLite shorthand
db = Database.from_url("postgresql://app:pw@db:5432/appdb")
await db.connect()
...
await db.aclose()
```

`db.dialect`, `db.config`, `db.path`, and `db.query_count` expose the backend name, parsed configuration, SQLite file path, and a per-instance query counter (handy in tests).

## See also

- [Plugins guide](../guides/plugins.md) — how plugin loading, ordering, and services work.
- [Configuration reference](../reference/configuration.md) — the `pyxle.config.json` schema.
- [pyxle-auth](pyxle-auth.md) — the official auth plugin, built on this contract.
