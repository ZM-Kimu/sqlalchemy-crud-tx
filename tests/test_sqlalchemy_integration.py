import pathlib
import sys
from typing import Generator

import pytest
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sqlalchemy_crud_tx import CRUD, SQLStatus

Base = declarative_base()


class SAUser(Base):  # type: ignore[misc]
    __tablename__ = "sa_user"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False)


@pytest.fixture(scope="function")
def sa_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", echo=False, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_pure_sqlalchemy_crud_basic(sa_session: Session) -> None:
    """Basic CRUD behaviour in a pure SQLAlchemy (non-Flask) setup."""

    CRUD.configure(session_provider=lambda: sa_session)

    # 创建
    with CRUD(SAUser) as crud:
        user = crud.add(email="sa@example.com")
        assert user is not None
        assert user.id is not None

    # 查询与更新
    with CRUD(SAUser, email="sa@example.com") as crud:
        found = crud.first()
        assert found is not None
        assert found.email == "sa@example.com"

        updated = crud.update(found, email="sa-updated@example.com")
        assert updated is not None
        assert updated.email == "sa-updated@example.com"

    # 删除
    with CRUD(SAUser, email="sa-updated@example.com") as crud:
        ok = crud.delete()
        assert ok is True
        assert crud.status == SQLStatus.OK

    # 确认已删除
    with CRUD(SAUser, email="sa-updated@example.com") as crud:
        assert crud.first() is None


def test_pure_sqlalchemy_transaction_join(sa_session: Session) -> None:
    """CRUD.transaction join semantics in a pure SQLAlchemy setup."""

    CRUD.configure(session_provider=lambda: sa_session)

    @CRUD.transaction()
    def create_two() -> None:
        with CRUD(SAUser) as c1:
            c1.add(email="join-a@example.com")
        with CRUD(SAUser) as c2:
            c2.add(email="join-b@example.com")

    create_two()

    with CRUD(SAUser) as crud:
        emails = {u.email for u in crud.query().all()}
        assert "join-a@example.com" in emails
        assert "join-b@example.com" in emails


def test_session_view_commit_and_rollback_redirect(sa_session: Session) -> None:
    """Session view should allow advanced operations but redirect commit/rollback to CRUD."""

    CRUD.configure(session_provider=lambda: sa_session)

    # 测试 commit 重定向
    with CRUD(SAUser) as crud:
        crud.add(email="view-commit@example.com")
        # 通过 session 视图调用 commit，应等价于 crud.commit()
        crud.session.commit()

    # 记录应已提交（直接通过 Session 查询验证）
    assert sa_session.query(SAUser).count() == 1
    # 确保没有遗留活动事务，避免下一次 begin 冲突
    sa_session.rollback()

    # 测试 rollback 重定向到 discard：在同一事务中撤销新增
    with CRUD(SAUser) as crud:
        crud.add(email="view-rollback@example.com")
        crud.session.rollback()

    # 第二条记录应被回滚，只剩一条（直接用 Session 查询）
    emails = {u.email for u in sa_session.query(SAUser).all()}
    assert "view-commit@example.com" in emails
    assert "view-rollback@example.com" not in emails


def test_existing_txn_policy_adopt_autobegin() -> None:
    """CRUD should tolerate AUTOBEGIN transactions when policy allows adoption."""
    engine = create_engine("sqlite:///:memory:", echo=False, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=True)
    session = SessionLocal()
    try:
        CRUD.configure(
            session_provider=lambda: session,
            existing_txn_policy="adopt_autobegin",
        )

        with CRUD(SAUser) as crud:
            user = crud.add(email="autobegin@example.com")
            assert user is not None

        # Access after commit triggers an AUTOBEGIN transaction.
        _ = user.email
        assert session.in_transaction()

        with CRUD(SAUser) as crud:
            found = crud.first()
            assert found is not None
    finally:
        session.close()
        engine.dispose()


def test_add_twice_same_crud_inserts_two_rows(sa_session: Session) -> None:
    """Repeated add() on the same CRUD object should insert new rows."""
    CRUD.configure(session_provider=lambda: sa_session, error_policy="raise")

    with CRUD(SAUser) as crud:
        first = crud.add(email="multi-1@example.com")
        second = crud.add(email="multi-2@example.com")
        assert first is not None
        assert second is not None
        assert first.id != second.id
        assert first.email == "multi-1@example.com"
        assert second.email == "multi-2@example.com"

    rows = sa_session.query(SAUser).order_by(SAUser.id).all()
    assert [r.email for r in rows] == ["multi-1@example.com", "multi-2@example.com"]


def test_reuse_crud_object_across_contexts_inserts_two_rows(sa_session: Session) -> None:
    """Reusing one CRUD instance across contexts should not reuse ORM rows."""
    CRUD.configure(session_provider=lambda: sa_session, error_policy="raise")

    crud = CRUD(SAUser)
    with crud:
        first = crud.add(email="reuse-1@example.com")
        assert first is not None

    with crud:
        second = crud.add(email="reuse-2@example.com")
        assert second is not None

    assert first.id != second.id
    rows = sa_session.query(SAUser).order_by(SAUser.id).all()
    assert [r.email for r in rows] == ["reuse-1@example.com", "reuse-2@example.com"]


def test_add_merges_before_updating_detached_source_object() -> None:
    """add(instance=..., **kwargs) should update managed row, not mutate detached source."""
    engine = create_engine("sqlite:///:memory:", echo=False, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    source_session = SessionLocal()
    target_session = SessionLocal()

    try:
        source_obj = SAUser(email="source@example.com")
        source_session.add(source_obj)
        source_session.commit()
        source_session.expunge(source_obj)

        CRUD.configure(session_provider=lambda: target_session, error_policy="raise")
        with CRUD(SAUser) as crud:
            merged = crud.add(instance=source_obj, email="managed@example.com")
            assert merged is not None
            assert merged.id == source_obj.id
            assert merged.email == "managed@example.com"

        assert source_obj.email == "source@example.com"
        saved = target_session.query(SAUser).filter_by(id=source_obj.id).first()
        assert saved is not None
        assert saved.email == "managed@example.com"
    finally:
        source_session.close()
        target_session.close()
        engine.dispose()


def test_add_unknown_field_raises_attribute_error(sa_session: Session) -> None:
    """Unknown update fields should fail fast instead of silently attaching attrs."""
    CRUD.configure(session_provider=lambda: sa_session, error_policy="raise")

    source = SAUser(email="known@example.com")
    with pytest.raises(AttributeError):
        with CRUD(SAUser) as crud:
            crud.add(instance=source, typo_field="x")


def test_paginate_without_flask_sqlalchemy(sa_session: Session) -> None:
    """paginate() should work without Flask-SQLAlchemy-specific Query extensions."""
    CRUD.configure(session_provider=lambda: sa_session, error_policy="raise")

    with CRUD(SAUser) as crud:
        for idx in range(1, 6):
            row = crud.add(email=f"page-{idx}@example.com")
            assert row is not None

    with CRUD(SAUser) as crud:
        page1 = crud.query().order_by(SAUser.id).paginate(page=1, per_page=2)
        assert [u.email for u in page1.items] == [
            "page-1@example.com",
            "page-2@example.com",
        ]
        assert page1.total == 5
        assert page1.pages == 3
        assert page1.has_prev is False
        assert page1.has_next is True
        assert page1.prev_num is None
        assert page1.next_num == 2

        page3 = crud.query().order_by(SAUser.id).paginate(page=3, per_page=2)
        assert [u.email for u in page3.items] == ["page-5@example.com"]
        assert page3.has_prev is True
        assert page3.has_next is False
        assert page3.prev_num == 2
        assert page3.next_num is None

        page2_no_count = crud.query().order_by(SAUser.id).paginate(
            page=2,
            per_page=2,
            count=False,
        )
        assert [u.email for u in page2_no_count.items] == [
            "page-3@example.com",
            "page-4@example.com",
        ]
        assert page2_no_count.total is None
        assert page2_no_count.pages == 0
        assert page2_no_count.has_prev is True
        assert page2_no_count.has_next is True
        assert page2_no_count.prev_num == 1
        assert page2_no_count.next_num == 3
