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
    ("applications", "pipeline_stage", "VARCHAR DEFAULT 'applied'"),
    ("applications", "user_notes", "TEXT DEFAULT ''"),
    ("applications", "follow_up_date", "VARCHAR DEFAULT ''"),
    ("applications", "user_id", "INTEGER"),
    ("seen_jobs", "user_id", "INTEGER"),
    ("companies", "user_id", "INTEGER"),
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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
