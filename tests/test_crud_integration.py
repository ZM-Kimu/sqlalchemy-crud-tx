import pathlib
import sys

import pytest
from sqlalchemy import func

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sqlalchemy.exc import SQLAlchemyError

from flask_sqlalchemy_crud import CRUD, SQLStatus  # noqa: E402


def _cleanup_all(app, db, model) -> None:
    with app.app_context():
        db.session.query(model).delete()
        db.session.commit()


def _cleanup_multi(app, db, *models) -> None:
    with app.app_context():
        for model in models:
            db.session.query(model).delete()
        db.session.commit()


def test_crud_add_query_update_delete(app_and_db) -> None:
    app, db, User, _Profile = app_and_db
    _cleanup_all(app, db, User)

    with app.app_context():
        CRUD.configure(session=db.session, error_policy="raise")

        email = "user@example.com"

        # 创建记录
        with CRUD(User) as crud:
            created = crud.add(email=email)
            assert created is not None
            assert created.id is not None

        # 查询与更新
        with CRUD(User, email=email) as crud:
            user = crud.first()
            assert user is not None
            assert user.email == email

            updated = crud.update(user, email="new@example.com")
            assert updated is not None
            assert updated.email == "new@example.com"

        # 删除
        with CRUD(User, email="new@example.com") as crud:
            deleted = crud.delete()
            assert deleted is True
            assert crud.status == SQLStatus.OK

        # 确认已删除
        with CRUD(User, email="new@example.com") as crud:
            assert crud.first() is None


def test_crud_transaction_decorator_join(app_and_db) -> None:
    app, db, User, _Profile = app_and_db
    _cleanup_all(app, db, User)

    with app.app_context():
        CRUD.configure(session=db.session, error_policy="raise")

        @CRUD.transaction()
        def create_two_users() -> None:
            with CRUD(User) as crud1:
                crud1.add(email="a@example.com")
            with CRUD(User) as crud2:
                crud2.add(email="b@example.com")

        create_two_users()

        # 两条记录应在同一事务内成功提交
        count = db.session.query(User).count()
        assert count >= 2


def test_on_sql_error_respects_error_policy(app_and_db) -> None:

    app, db, User, _Profile = app_and_db

    with app.app_context():
        CRUD.configure(session=db.session)

        # status 策略：不抛出异常，只设置状态
        crud_status = CRUD(User).config(error_policy="status")
        crud_status._on_sql_error(SQLAlchemyError("test"))  # type: ignore[attr-defined]  # noqa: SLF001
        assert crud_status.status == SQLStatus.SQL_ERR

        # raise 策略：应抛出 SQLAlchemyError
        crud_raise = CRUD(User).config(error_policy="raise")
        with pytest.raises(SQLAlchemyError):
            crud_raise._on_sql_error(SQLAlchemyError("test"))  # type: ignore[attr-defined]  # noqa: SLF001


def test_transaction_rollback_on_runtime_exception(app_and_db) -> None:
    """外层事务内抛出非 SQLAlchemy 异常，应整体回滚。"""
    app, db, User, _Profile = app_and_db
    _cleanup_all(app, db, User)

    with app.app_context():
        CRUD.configure(session=db.session)

        @CRUD.transaction()
        def create_then_fail() -> None:
            with CRUD(User) as crud:
                crud.add(email="rollback@example.com")
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            create_then_fail()

        assert db.session.query(User).count() == 0


def test_nested_transaction_join_and_rollback(app_and_db) -> None:
    """内外层装饰器 join 同一事务，外层异常导致整体回滚。"""
    app, db, User, Profile = app_and_db
    _cleanup_multi(app, db, User, Profile)

    with app.app_context():
        CRUD.configure(session=db.session)

        @CRUD.transaction()
        def inner_create(email: str) -> None:
            with CRUD(User) as crud_user:
                user = crud_user.add(email=email)
                assert user is not None
            with CRUD(Profile) as crud_profile:
                crud_profile.add(user_id=user.id, bio="nested")  # type: ignore[arg-type]

        @CRUD.transaction()
        def outer() -> None:
            inner_create("nested@example.com")
            raise RuntimeError("outer failed")

        with pytest.raises(RuntimeError):
            outer()

        assert db.session.query(User).count() == 0
        assert db.session.query(Profile).count() == 0


def test_status_policy_rolls_back_without_raising(app_and_db) -> None:
    """status 策略下 SQLAlchemyError 不抛出，但事务仍应回滚。"""

    app, db, User, Profile = app_and_db
    _cleanup_multi(app, db, User, Profile)

    with app.app_context():
        CRUD.configure(session=db.session)

        @CRUD.transaction(error_policy="status")
        def create_then_force_sql_error() -> None:
            with CRUD(User).config(error_policy="status") as crud:
                crud.add(email="status@example.com")
                # 模拟一次 SQLAlchemyError，以触发 _on_sql_error 中的回滚逻辑
                crud._on_sql_error(SQLAlchemyError("forced"))  # type: ignore[attr-defined]  # noqa: SLF001

        # 不应抛出异常
        create_then_force_sql_error()

        # 记录应被回滚
        assert db.session.query(User).count() == 0


def test_discard_rolls_back_partial_operations(app_and_db) -> None:
    """在同一事务中通过 discard 回滚部分操作。"""
    app, db, User, Profile = app_and_db
    _cleanup_multi(app, db, User, Profile)

    with app.app_context():
        CRUD.configure(session=db.session)

        @CRUD.transaction()
        def mixed_ops() -> None:
            # 第一段：应保留
            with CRUD(User) as crud_keep:
                user = crud_keep.add(email="keep@example.com")
                assert user is not None

            # 第二段：为同一用户创建 profile，并通过 discard 回滚
            with CRUD(Profile) as crud_profile:
                crud_profile.add(user_id=user.id, bio="to-be-discarded")  # type: ignore[arg-type]
                crud_profile.discard()

        mixed_ops()

        users = {u.email for u in db.session.query(User).all()}
        profiles = list(db.session.query(Profile).all())

        assert "keep@example.com" in users
        assert len(profiles) == 0


def test_query_count_order_by_group_by(app_and_db) -> None:
    """测试 CRUDQuery 的 count / order_by / group_by 等常用链式操作。"""
    app, db, User, Profile = app_and_db
    _cleanup_multi(app, db, User, Profile)

    with app.app_context():
        CRUD.configure(session=db.session, error_policy="raise")

        emails = [
            "alpha@example.com",
            "beta@example.com",
            "gamma@example.com",
            "delta@example.com",
        ]

        # 预置多条记录（每条记录使用独立 CRUD 实例，避免复用同一 instance）
        for email in emails:
            with CRUD(User) as crud_seed:
                crud_seed.add(email=email)

        with CRUD(User) as crud:
            # count：应与插入条数一致
            base_query = crud.query()
            assert base_query.count() == len(emails)

            # order_by：按 email 倒序排列
            ordered = crud.query().order_by(User.email.desc()).all()
            ordered_emails = [u.email for u in ordered]
            assert ordered_emails == sorted(emails, reverse=True)

            # group_by：按 id 的奇偶性分组，并统计每组数量
            group_query = (
                crud.query()
                .with_entities(func.mod(User.id, 2).label("grp"), func.count(User.id))
                .group_by("grp")
            )
            grouped = group_query.all()

            total = sum(count for _grp, count in grouped)
            assert total == len(emails)
            # 理论上会有 1~2 个分组（全偶 / 全奇 / 混合），只需要确认 group_by 正常工作
            assert 1 <= len(grouped) <= 2
