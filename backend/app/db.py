import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DB_PATH

# DATABASE_URL (any SQLAlchemy URL) wins; otherwise the local SQLite file.
DATABASE_URL = os.environ.get("DATABASE_URL") or f"sqlite:///{DB_PATH}"
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


# Columns added after the first release; create_all does not alter existing
# tables, so each is added here when missing. (table, column, SQL type)
_ADDED_COLUMNS = [
    ("runs", "browser_fill", "INTEGER DEFAULT 0"),
    ("runs", "user_id", "INTEGER"),
    ("job_matches", "posted_date", "VARCHAR DEFAULT ''"),
    ("applications", "pipeline_stage", "VARCHAR DEFAULT 'not_applied'"),
    ("applications", "user_notes", "TEXT DEFAULT ''"),
    ("applications", "follow_up_date", "VARCHAR DEFAULT ''"),
    ("applications", "user_id", "INTEGER"),
    ("seen_jobs", "user_id", "INTEGER"),
    ("companies", "user_id", "INTEGER"),
    ("profiles", "first_name", "VARCHAR DEFAULT ''"),
    ("profiles", "last_name", "VARCHAR DEFAULT ''"),
]


def upgrade_schema() -> None:
    """Add columns introduced after a database was first created (create_all won't alter tables)."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl in _ADDED_COLUMNS:
            if table not in tables:
                continue
            cols = {c["name"] for c in inspector.get_columns(table)}
            if column not in cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        # Correct pipeline stages stamped by the old default: every packet
        # used to start at "applied", even ones that were never submitted.
        # An "applied" stage (or an unset one) only survives on rows whose
        # status is "applied" — the one status that means the application
        # actually went out. Stages the user moved past "applied"
        # themselves are never touched. Idempotent: corrected rows no
        # longer match either statement, so later startups are no-ops.
        if "applications" in tables:
            cols = {c["name"] for c in inspect(conn).get_columns("applications")}
            if {"pipeline_stage", "status"} <= cols:
                conn.execute(
                    text(
                        "UPDATE applications SET pipeline_stage = 'not_applied' "
                        "WHERE COALESCE(pipeline_stage, '') IN ('', 'applied') "
                        "AND COALESCE(status, '') <> 'applied'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE applications SET pipeline_stage = 'applied' "
                        "WHERE COALESCE(pipeline_stage, '') = '' AND status = 'applied'"
                    )
                )
        # Backfill the first/last split for profiles stored before the
        # split existed. Idempotent: rows that already carry a split are
        # left alone, so this is a no-op on every later startup.
        if "profiles" in tables:
            cols = {c["name"] for c in inspect(conn).get_columns("profiles")}
            if {"name", "first_name", "last_name"} <= cols:
                from .agent.profile import split_name

                rows = conn.execute(
                    text(
                        "SELECT id, name FROM profiles "
                        "WHERE COALESCE(first_name, '') = '' "
                        "AND COALESCE(last_name, '') = '' "
                        "AND COALESCE(name, '') <> ''"
                    )
                ).fetchall()
                for row_id, full_name in rows:
                    first, last = split_name(full_name)
                    conn.execute(
                        text("UPDATE profiles SET first_name = :f, last_name = :l WHERE id = :i"),
                        {"f": first, "l": last, "i": row_id},
                    )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
