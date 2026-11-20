"""Idempotent column additions for databases created by an earlier release.

SQLAlchemy's create_all only creates missing tables, not missing columns.  The
demo keeps its schema in code, so this short list of ALTER statements upgrades
an existing PostgreSQL volume in place.  Fresh databases get everything from
create_all and skip every statement.
"""

from sqlalchemy import text

from src.config.database import engine

_STATEMENTS = [
    "ALTER TABLE samples ADD COLUMN IF NOT EXISTS parent_id INTEGER REFERENCES samples(id)",
    "ALTER TABLE samples ADD COLUMN IF NOT EXISTS allocation_batch_id INTEGER",
    "ALTER TABLE samples ADD COLUMN IF NOT EXISTS accepting BOOLEAN NOT NULL DEFAULT true",
    "ALTER TABLE samples ADD COLUMN IF NOT EXISTS transfer_id INTEGER",
    "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'pending'",
    "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS allocation_batch_id INTEGER",
    "ALTER TABLE transfers ADD COLUMN IF NOT EXISTS source_sample_id INTEGER REFERENCES samples(id)",
    "ALTER TABLE allocation_batches ADD COLUMN IF NOT EXISTS optimal BOOLEAN NOT NULL DEFAULT true",
]


def upgrade_schema():
    if not engine.url.drivername.startswith('postgresql'):
        return
    with engine.begin() as connection:
        for statement in _STATEMENTS:
            connection.execute(text(statement))
