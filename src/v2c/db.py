"""Short DB transactions. PostgreSQL row locking is the production protocol."""
from contextlib import contextmanager
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


def make_engine(url):
    options = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if ":memory:" in url:
            options["poolclass"] = StaticPool
    engine = create_engine(url, **options)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def sqlite_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys = ON")
    return engine


@contextmanager
def transaction(engine):
    with Session(engine, expire_on_commit=False) as session:
        try:
            if engine.dialect.name == "sqlite":
                # SQLite author tests serialize writers; this does not test PG locks.
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
