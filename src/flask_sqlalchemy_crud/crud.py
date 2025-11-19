from __future__ import annotations

import logging
from functools import wraps
from typing import (
    Any,
    Generic,
    Literal,
    Optional,
    Self,
    cast,
    overload,
    ParamSpec,
    TypeVar,
)
from flask_sqlalchemy.model import Model
from flask_sqlalchemy.query import Query
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import object_session
from sqlalchemy.sql import _orm_types

from .query import CRUDQuery
from .status import SQLStatus
from .types import ErrorLogger, ModelTypeVar, SessionLike
from .transaction import (
    ErrorPolicy,
    TransactionDecorator,
    _get_or_create_txn_state,
    _get_txn_state,
    get_current_error_policy,
    transaction as _txn_transaction,
)

P = ParamSpec("P")
R = TypeVar("R")

_error_logger: ErrorLogger = logging.getLogger("CRUD").error


def _get_session_for_cls(crud_cls: type["CRUD"]) -> SessionLike:
    """根据 CRUD 类获取当前配置的会话对象。

    依赖外部通过 CRUD.configure 预先设置 session。
    """
    session = getattr(crud_cls, "session", None)
    if session is None:
        raise RuntimeError(
            "CRUD session is not configured. "
            "Please call CRUD.configure(session=...) before using CRUD."
        )
    return cast(SessionLike, session)


class CRUD(Generic[ModelTypeVar]):
    """通用 CRUD 封装。

    - 基于上下文管理器的事务提交/回滚。
    - 统一错误状态管理（SQLStatus）。
    - 全局与实例级默认过滤条件。
    """

    _global_filter_conditions: tuple[list, dict] = ([], {})
    session: SessionLike | None = None
    _default_error_policy: ErrorPolicy = "raise"

    @classmethod
    def register_global_filters(cls, *base_exprs, **base_kwargs) -> None:
        """为所有模型注册全局基础过滤。"""
        cls._global_filter_conditions = (list(base_exprs) or []), (base_kwargs or {})

    def __init__(self, model: type[Model], **kwargs) -> None:
        self._model = model
        self._kwargs = kwargs

        self.instance: Model | None = None
        self._base_filter_exprs: list = list(self._global_filter_conditions[0])
        self._base_filter_kwargs: dict = dict(self._global_filter_conditions[1])
        self._instance_default_kwargs: dict = dict(kwargs)

        self.error: Exception | None = None
        self.status: SQLStatus = SQLStatus.OK

        self._need_commit = False
        self._error_policy: ErrorPolicy | None = None
        self._apply_global_filters = True
        self._txn_state = None
        self._joined_txn = False
        self._sub_txn = None
        self._explicit_committed = False
        self._discarded = False

    def _resolve_error_policy(self) -> ErrorPolicy:
        """解析当前 CRUD 实例应采用的 error_policy。

        优先级：
        1. 当前事务装饰器上下文中设置的 error_policy（若有）；
        2. 实例级配置（config）；
        3. 类级默认配置（set_config / _default_error_policy）。
        """
        from_ctx = get_current_error_policy()
        if from_ctx is not None:
            return from_ctx
        if self._error_policy is not None:
            return self._error_policy
        return self._default_error_policy

    def __enter__(self) -> Self:
        assert self.session is not None
        session = self.session

        state = _get_txn_state(session)
        joined_existing = bool(state is not None and state.active)

        if not joined_existing:
            state = _get_or_create_txn_state(session)
            state.depth = 0
            state.active = True
            try:
                session.begin()
            except Exception:
                state.active = False
                raise

        assert state is not None
        state.depth += 1

        self._txn_state = state
        self._joined_txn = joined_existing
        self._explicit_committed = False
        self._discarded = False
        return self

    @classmethod
    def configure(
        cls,
        *,
        session: SessionLike | None = None,
        error_logger: ErrorLogger | None = None,
    ) -> None:
        """配置 CRUD 所依赖的会话与日志函数（类级别）。"""
        if session is not None:
            cls.session = session
        if error_logger is not None:
            global _error_logger
            _error_logger = error_logger

    def config(
        self,
        error_policy: ErrorPolicy | None = None,
        disable_global_filter: bool | None = None,
    ) -> Self:
        if error_policy is not None:
            self._error_policy = error_policy
        if disable_global_filter is not None:
            self._apply_global_filters = not disable_global_filter
        return self

    @classmethod
    def set_config(cls, *, error_policy: ErrorPolicy | None = None) -> None:
        """配置 CRUD 类级默认行为（如 error_policy）。"""
        if error_policy is not None:
            cls._default_error_policy = error_policy

    def create_instance(self, no_attach: bool = False) -> ModelTypeVar:
        if no_attach:
            return cast(ModelTypeVar, self._model(**self._kwargs))
        if self.instance is None:
            self.instance = self._model(**self._kwargs)
        return cast(ModelTypeVar, self.instance)

    @overload
    def add(self, instances: None = None, **kwargs) -> Optional[ModelTypeVar]: ...

    @overload
    def add(
        self, instances: list[ModelTypeVar], **kwargs
    ) -> Optional[list[ModelTypeVar]]: ...

    @overload
    def add(self, instances: ModelTypeVar, **kwargs) -> Optional[ModelTypeVar]: ...

    def add(
        self, instances: ModelTypeVar | list[ModelTypeVar] | None = None, **kwargs
    ) -> list[ModelTypeVar] | ModelTypeVar | None:
        try:
            instances = instances or self.create_instance()
            if not isinstance(instances, list):
                instances = [instances]

            self._ensure_sub_txn()

            managed_instances: list[ModelTypeVar] = []
            for instance in instances:
                need_merge = False
                try:
                    if not (insp := sa_inspect(instance)):
                        raise ValueError()
                    bound_sess = object_session(instance)
                    need_merge = (not insp.transient) or (
                        bound_sess is not None and bound_sess is not self.session
                    )
                except Exception:
                    need_merge = True

                assert self.session is not None
                target = (
                    cast(ModelTypeVar, self.session.merge(instance))
                    if need_merge
                    else instance
                )
                if updated := self.update(target, **kwargs):
                    managed_instances.append(updated)
                else:
                    managed_instances.append(target)

            assert self.session is not None
            self.session.add_all(managed_instances)
            self._need_commit = True
            self._mark_dirty()
            return (
                managed_instances[0]
                if len(managed_instances) == 1
                else managed_instances
            )
        except SQLAlchemyError as e:
            self._on_sql_error(e)
        except Exception as e:
            self.error = e
            self.status = SQLStatus.INTERNAL_ERR
        return None

    def query(
        self, *args, pure: bool = False, **kwargs
    ) -> CRUDQuery[ModelTypeVar, ModelTypeVar]:
        query = cast(Query, self._model.query)
        if not pure:
            if self._instance_default_kwargs:
                query = query.filter_by(**self._instance_default_kwargs)
            if self._apply_global_filters:
                if self._base_filter_exprs:
                    query = query.filter(*self._base_filter_exprs)
                if self._base_filter_kwargs:
                    query = query.filter_by(**self._base_filter_kwargs)

        final_query = query
        try:
            if args:
                final_query = final_query.filter(*args)
            if kwargs:
                final_query = final_query.filter_by(**kwargs)
        except SQLAlchemyError as e:
            self._on_sql_error(e)
            self._log(e, self.status)
        except Exception as e:
            self.error = e
            self.status = SQLStatus.INTERNAL_ERR
            self._log(e, self.status)
        return CRUDQuery(self, final_query)

    def first(
        self, query: CRUDQuery[ModelTypeVar, ModelTypeVar] | None = None
    ) -> ModelTypeVar | None:
        if query is None:
            query = self.query()
        return query.first()

    def all(
        self, query: CRUDQuery[ModelTypeVar, ModelTypeVar] | None = None
    ) -> list[ModelTypeVar]:
        if query is None:
            query = self.query()
        return query.all()

    def update(
        self, instance: ModelTypeVar | None = None, **kwargs
    ) -> ModelTypeVar | None:
        try:
            if instance is None:
                instance = self.query().first()

            if not instance:
                return None

            self._ensure_sub_txn()
            assert self.session is not None
            with self.session.no_autoflush:
                for k, v in kwargs.items():
                    setattr(instance, k, v)
            self._need_commit = True
            self._mark_dirty()
            return instance
        except SQLAlchemyError as e:
            self._on_sql_error(e)
        except Exception as e:
            self.error = e
            self.status = SQLStatus.INTERNAL_ERR
        return None

    def delete(
        self,
        instance: ModelTypeVar | None = None,
        query: CRUDQuery[ModelTypeVar, ModelTypeVar] | None = None,
        all_records: bool = False,
        sync: _orm_types.SynchronizeSessionArgument = "fetch",
    ) -> bool:
        try:
            assert self.session is not None
            if instance:
                self._ensure_sub_txn()
                self.session.delete(instance)
            else:
                if query is None:
                    query = self.query()

                first_inst = query.first()
                if not first_inst:
                    self.status = SQLStatus.NOT_FOUND
                    return False

                self._ensure_sub_txn()
                if all_records:
                    query.delete(synchronize_session=sync)
                else:
                    self.session.delete(first_inst)

            self._need_commit = True
            self._mark_dirty()
            return True
        except SQLAlchemyError as e:
            self._on_sql_error(e)
        except Exception as e:
            self.error = e
            self.status = SQLStatus.INTERNAL_ERR
        return False

    def need_commit(self) -> None:
        self._ensure_sub_txn()
        self._need_commit = True
        self._mark_dirty()

    def commit(self) -> None:
        try:
            assert self.session is not None
            if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                self._sub_txn.commit()
            else:
                self.session.commit()
            self._explicit_committed = True
            self._need_commit = False
        except Exception as e:
            _error_logger(f"CRUD commit failed: {e}")
            if self.session is not None:
                self.session.rollback()

    def discard(self) -> None:
        try:
            assert self.session is not None
            if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                self._sub_txn.rollback()
            else:
                self.session.rollback()
        finally:
            self.error = AssertionError("User called rollback.")
            self._need_commit = False
            self._discarded = True

    def _log(self, error: Exception, status: SQLStatus = SQLStatus.INTERNAL_ERR):
        model_name = getattr(self._model, "__name__", str(self._model))
        _error_logger(f"CRUD[{model_name}]: <catch: {error}> <except: ({status})>")

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # 对于非 SQLAlchemy 异常，始终向外抛出；
        # SQLAlchemyError 的抛出与否由 error_policy 决定，
        # 在事务装饰器或 _on_sql_error 中处理。
        if self.error and not isinstance(self.error, SQLAlchemyError):
            raise self.error
        try:
            has_exc = bool(exc_type or exc_val or exc_tb)
            should_rollback = has_exc or self.error is not None or self._discarded

            if should_rollback:
                if has_exc or self.error:
                    model_name = getattr(self._model, "__name__", str(self._model))
                    _error_logger(
                        f"CRUD[{model_name}]: <catch: {self.error}> "
                        f"<except: ({exc_type}: {exc_val})>"
                    )
                try:
                    if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                        self._sub_txn.rollback()
                except Exception:
                    pass
                self._need_commit = False
            elif self._need_commit and not self._explicit_committed:
                try:
                    if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                        self._sub_txn.commit()
                except Exception as e:
                    _error_logger(f"CRUD commit failed: {e}")
                    raise

            # 基于通用事务状态机调整深度，并在最外层执行提交/回滚
            if self.session is not None:
                session = self.session
                state = _get_txn_state(session)
                joined_existing = getattr(self, "_joined_txn", False)

                if state is not None and state.active:
                    state.depth -= 1
                    is_outermost = state.depth <= 0
                    if is_outermost:
                        state.active = False
                        try:
                            if should_rollback and not joined_existing:
                                session.rollback()
                            elif (
                                self._need_commit
                                and not self._explicit_committed
                                and not joined_existing
                            ):
                                session.commit()
                        except Exception as e:
                            _error_logger(f"CRUD commit/rollback failed: {e}")
                            try:
                                session.rollback()
                            except Exception:
                                pass
                            raise e
        finally:
            # Session 的生命周期由外部（如应用框架）负责管理。
            return

    def _ensure_sub_txn(self) -> None:
        if not (self._sub_txn and self._sub_txn.is_active):
            try:
                assert self.session is not None
                self._sub_txn = self.session.begin_nested()
            except Exception:
                self._sub_txn = None

    def _mark_dirty(self) -> None:
        # 当前上下文事务由通用状态机管理，保留占位以便未来扩展。
        return

    def _on_sql_error(self, e: Exception) -> None:
        self.error = e
        self.status = SQLStatus.SQL_ERR
        try:
            assert self.session is not None
            if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                self._sub_txn.rollback()
            else:
                self.session.rollback()
        except Exception:
            pass
        self._need_commit = False
        # 仅当 error_policy 为 "raise" 时，对 SQLAlchemy 异常向外抛出，
        # 由事务装饰器或调用方统一处理。
        if self._resolve_error_policy() == "raise":
            raise e

    @classmethod
    def transaction(
        cls,
        *,
        error_policy: ErrorPolicy | None = None,
        join: bool = True,
        nested: bool | None = None,
    ) -> TransactionDecorator[P, R]:
        """函数级事务装饰器。

        - 一次函数调用 = 一个 CRUD 相关的事务域。
        - 基于通用 transaction(...) 实现 join 语义与提交/回滚。
        """

        resolved_policy: ErrorPolicy = (
            error_policy if error_policy is not None else cls._default_error_policy
        )

        def session_factory() -> SessionLike:
            return _get_session_for_cls(cls)

        return _txn_transaction(
            session_factory,
            join=join,
            nested=nested,
            error_policy=resolved_policy,
        )
