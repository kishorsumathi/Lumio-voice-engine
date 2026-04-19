"""
One-shot schema bootstrap — runs SQLAlchemy ``Base.metadata.create_all``.

Use this once per environment (local Postgres, RDS, etc.) to create the
``jobs``, ``segments``, ``translations`` tables defined in
``worker/src/pipeline/models.py``. It is idempotent — safe to re-run.

For prod RDS:

    DATABASE_URL='postgresql+psycopg2://user:pass@host:5432/dbname' \
        uv run python scripts/init_db.py

Note: this is NOT a migration tool. If you ever need to ALTER a column or
add a constraint to an already-populated table, you'll need to write the SQL
by hand or bring Alembic back. For the current "transcribe audio, store
results" schema that's unlikely.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "worker" / "src"))

from pipeline.db import create_tables, health_check  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    if not health_check():
        print("ERROR: database unreachable — check DATABASE_URL", file=sys.stderr)
        return 1
    create_tables()
    print("Schema ready (jobs, segments, translations).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
