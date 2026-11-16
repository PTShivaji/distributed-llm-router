"""Minimal versioned migration runner (no Alembic in this project).

- Migrations are ordered (version, name, callable) and recorded in
  `schema_migrations`; each runs once, inside a transaction.
- A PostgreSQL advisory lock serialises concurrent startups, so several
  router instances booting together cannot race or double-apply.
- Only creates/alters objects; never drops data.
- Future schema changes: append a new Migration with explicit DDL. If the
  project adopts Alembic later, stamp the current head and continue there.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, select, text

from app.core.logging import log_event
from app.db.models import Base, ProviderAttempt, RequestLog, SchemaMigration

logger = logging.getLogger("app.db.migrations")

ADVISORY_LOCK_ID = 7_340_031  # arbitrary constant, unique to this application


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[Connection], None]


def _v1_initial(conn: Connection) -> None:
    Base.metadata.create_all(conn, tables=[RequestLog.__table__, ProviderAttempt.__table__])


MIGRATIONS: list[Migration] = [
    Migration(1, "create request_log and provider_attempt", _v1_initial),
]


def run_migrations(engine: Engine) -> list[int]:
    """Apply pending migrations. Returns the versions applied by this call."""
    applied_now: list[int] = []
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": ADVISORY_LOCK_ID})
        try:
            Base.metadata.create_all(conn, tables=[SchemaMigration.__table__])
            conn.commit()
            done = set(conn.execute(select(SchemaMigration.version)).scalars())
            conn.commit()  # end the autobegun read transaction; the advisory lock is session-level
            for m in sorted(MIGRATIONS, key=lambda m: m.version):
                if m.version in done:
                    continue
                with conn.begin():
                    m.apply(conn)
                    conn.execute(SchemaMigration.__table__.insert().values(version=m.version, name=m.name))
                applied_now.append(m.version)
                log_event(logger, "db.migration_applied", version=m.version, name=m.name)
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": ADVISORY_LOCK_ID})
            conn.commit()
    return applied_now
