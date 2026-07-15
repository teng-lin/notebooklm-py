from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.expanduser("~/.notebooklm/data/baoku.db")
NOTEBOOKLM_DB_URL = os.environ.get(
    "NOTEBOOKLM_DATABASE_URL",
    f"sqlite:///{DEFAULT_DB_PATH}",
)

engine: Any = None
SessionLocal: sessionmaker[Session] | None = None


class Base(DeclarativeBase):
    pass


def get_db_path() -> str:
    if NOTEBOOKLM_DB_URL.startswith("sqlite:///"):
        return NOTEBOOKLM_DB_URL[len("sqlite:///"):]
    return NOTEBOOKLM_DB_URL


def init_db(db_url: str | None = None) -> None:
    global engine, SessionLocal
    url = db_url or NOTEBOOKLM_DB_URL
    if url.startswith("sqlite"):
        db_path = url[len("sqlite:///"):]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, echo=False, future=True)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialized at %s", url)


def get_session() -> Session:
    if SessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return SessionLocal()


def close_db() -> None:
    global engine, SessionLocal
    if engine is not None:
        engine.dispose()
    engine = None
    SessionLocal = None
