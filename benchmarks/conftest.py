from __future__ import annotations

import os
import pathlib
import sys
from collections.abc import Callable, Generator
from itertools import count
from uuid import uuid4

import pytest
from sqlalchemy import Column, String, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sqlalchemy_crud_tx import CRUD

Base = declarative_base()


class BenchUser(Base):  # type: ignore[misc]
    __tablename__ = "bench_user"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    email = Column(String(255), unique=True, nullable=False)


def _load_bench_db_uri() -> str:
    uri = os.getenv("BENCH_DB", "sqlite+pysqlite:///:memory:")
    if uri.startswith("mysql://"):
        uri = "mysql+pymysql://" + uri[len("mysql://") :]
    return uri


@pytest.fixture(scope="session")
def bench_engine():
    engine = create_engine(_load_bench_db_uri(), echo=False, future=True)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture(scope="session")
def SessionLocal(bench_engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=bench_engine,
        class_=Session,
        expire_on_commit=False,
        autoflush=True,
    )


@pytest.fixture(scope="function")
def sa_session(SessionLocal: sessionmaker[Session]) -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        session.query(BenchUser).delete()
        session.commit()
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(scope="function")
def configured_crud(sa_session: Session) -> None:
    CRUD.configure(
        session_provider=lambda: sa_session,
        error_policy="raise",
        existing_txn_policy="adopt_autobegin",
    )


@pytest.fixture(scope="function")
def email_factory() -> Callable[[str], str]:
    seq = count()

    def _new(prefix: str) -> str:
        return f"{prefix}-{next(seq)}@bench.local"

    return _new


@pytest.fixture(scope="function")
def seeded_user(
    sa_session: Session,
    email_factory: Callable[[str], str],
) -> tuple[str, str]:
    user = BenchUser(email=email_factory("seed"))
    sa_session.add(user)
    sa_session.commit()
    return user.id, user.email


@pytest.fixture(scope="function")
def seeded_user_id(seeded_user: tuple[str, str]) -> str:
    return seeded_user[0]


@pytest.fixture(scope="function")
def seeded_user_email(seeded_user: tuple[str, str]) -> str:
    return seeded_user[1]


@pytest.fixture(scope="function")
def seeded_many_users(
    sa_session: Session,
    email_factory: Callable[[str], str],
) -> list[str]:
    rows = [BenchUser(email=email_factory("seed-many")) for _ in range(200)]
    sa_session.add_all(rows)
    sa_session.commit()
    return [row.id for row in rows]


@pytest.fixture(scope="session")
def bench_user_model() -> type[BenchUser]:
    return BenchUser
