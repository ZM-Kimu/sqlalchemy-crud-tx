"""CRUD 辅助与事务集成实现。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Generic, ParamSpec, Self, TypeVar, cast

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Query, object_session
from sqlalchemy.sql import _orm_types

from .query import CRUDQuery
from .status import SQLStatus
from .transaction import (
    ErrorPolicy,
    TransactionDecorator,
    _get_or_create_txn_state,
    _get_txn_state,
    _TxnState,
    get_current_error_policy,
)
from .transaction import transaction as _txn_transaction
from .types import ErrorLogger, ModelTypeVar, QueryFactory, SessionLike, SessionProvider

P = ParamSpec("P")
R = TypeVar("R")

_DEFAULT_LOGGER: ErrorLogger = logging.getLogger("CRUD").error


def _default_query_factory(
    model: type[ModelTypeVar], session: SessionLike, crud: "CRUD[ModelTypeVar]"
) -> CRUDQuery[ModelTypeVar, ModelTypeVar]:
    """默认使用 SQLAlchemy Session 构造查询（不依赖 Flask-SQLAlchemy）。"""
    base_query = session.query(model)
    return CRUDQuery(crud, cast(Query, base_query))


class _SessionView:
    """对外暴露的 Session 视图。

    - 普通方法直接透传到底层 Session；
    - commit/rollback 调用被重定向到 CRUD.commit / CRUD.discard，
      避免绕过 CRUD 自己的事务状态机。
    """

    __slots__ = ("_crud", "_session")

    def __init__(self, crud: "CRUD[Any]", session: SessionLike) -> None:
        self._crud = crud
        self._session = session

    def commit(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401 - 简短重定向
        """重定向到 CRUD.commit，并记录日志提示。"""
        self._crud._logger(  # noqa: SLF001
            "CRUD.session.commit() is redirected to CRUD.commit(); "
            "consider calling CRUD.commit() explicitly.",
        )
        self._crud.commit()

    def rollback(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401 - 简短重定向
        """重定向到 CRUD.discard，并记录日志提示。"""
        self._crud._logger(  # noqa: SLF001
            "CRUD.session.rollback() is redirected to CRUD.discard(); "
            "consider calling CRUD.discard() explicitly.",
        )
        self._crud.discard()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


if TYPE_CHECKING:
    SessionViewType = SessionLike
else:
    SessionViewType = _SessionView


class CRUD(Generic[ModelTypeVar]):  # pylint: disable=too-many-instance-attributes
    """通用 CRUD 封装。

    - 基于上下文管理器的事务提交/回滚。
    - 统一错误状态管理（SQLStatus）。
    - 全局与实例级默认过滤条件。
    """

    _global_filter_conditions: tuple[list, dict] = ([], {})
    _session_provider: SessionProvider | None = None
    _query_factory: QueryFactory | None = None
    _default_error_policy: ErrorPolicy = "raise"
    _logger: ErrorLogger = _DEFAULT_LOGGER

    @classmethod
    def register_global_filters(cls, *base_exprs, **base_kwargs) -> None:
        """为所有模型注册全局基础过滤。"""
        cls._global_filter_conditions = (list(base_exprs) or []), (base_kwargs or {})

    def __init__(self, model: type[ModelTypeVar], **kwargs: Any) -> None:
        """初始化 CRUD 实例。"""
        self._model = model
        self._kwargs = kwargs

        self.instance: ModelTypeVar | None = None
        self._base_filter_exprs: list = list(self._global_filter_conditions[0])
        self._base_filter_kwargs: dict = dict(self._global_filter_conditions[1])
        self._instance_default_kwargs: dict = dict(kwargs)

        self.error: Exception | None = None
        self.status: SQLStatus = SQLStatus.OK

        self._need_commit = False
        self._error_policy: ErrorPolicy | None = None
        self._apply_global_filters = True
        self._txn_state: _TxnState | None = None
        self._joined_txn = False
        self._sub_txn = None
        self._explicit_committed = False
        self._discarded = False
        self._session: SessionLike | None = None

    def _resolve_error_policy(self) -> ErrorPolicy:
        """解析当前 CRUD 实例应采用的 error_policy。

        优先级：
        1. 当前事务装饰器上下文中设置的 error_policy（若有）；
        2. 实例级配置（config）；
        3. 类级默认配置（_default_error_policy）。
        """
        from_ctx = get_current_error_policy()
        if from_ctx is not None:
            return from_ctx
        if self._error_policy is not None:
            return self._error_policy
        return self._default_error_policy

    def __enter__(self) -> Self:
        """进入上下文管理器并加入/创建事务域。"""
        session = self._get_session()

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
        self._session = session
        self._joined_txn = joined_existing
        self._explicit_committed = False
        self._discarded = False
        return self

    @classmethod
    def configure(
        cls,
        *,
        session: SessionLike | None = None,
        session_provider: SessionProvider | None = None,
        query_factory: QueryFactory | None = None,
        error_logger: ErrorLogger | None = None,
        error_policy: ErrorPolicy | None = None,
    ) -> None:
        """配置 CRUD 所依赖的会话 provider、查询构造与日志和默认策略（类级别）。"""
        if session is not None and session_provider is not None:
            raise ValueError("Provide either session or session_provider, not both.")

        if session_provider is not None:
            cls._session_provider = session_provider
        elif session is not None:
            cls._session_provider = lambda: session
        else:
            raise ValueError(
                "session_provider is required (or pass session to be wrapped)."
            )

        if query_factory is not None:
            cls._query_factory = query_factory

        if error_logger is not None:
            cls._logger = error_logger
        if error_policy is not None:
            cls._default_error_policy = error_policy

    @classmethod
    def _get_session_provider(cls) -> SessionProvider:
        if cls._session_provider is None:
            raise RuntimeError(
                "CRUD session is not configured. Please call "
                "CRUD.configure(session_provider=...) before using CRUD."
            )
        return cls._session_provider

    def _get_session(self) -> SessionLike:
        provider = self._get_session_provider()
        return cast(SessionLike, provider())

    def _get_query_factory(self) -> QueryFactory:
        if self._query_factory is not None:
            return self._query_factory
        if type(self)._query_factory is not None:
            return cast(QueryFactory, type(self)._query_factory)
        return lambda model, session: _default_query_factory(model, session, self)

    def _require_session(self) -> SessionLike:
        if self._session is None:
            raise RuntimeError("CRUD session is not bound to current context.")
        return self._session

    @property
    def session(self) -> SessionViewType:
        """返回一个受控 Session 视图。

        - 只应在 with CRUD(...) 上下文内部使用；
        - commit/rollback 会被重定向到 CRUD.commit / CRUD.discard。
        """
        session = self._require_session()
        return _SessionView(self, session)

    def config(
        self,
        error_policy: ErrorPolicy | None = None,
        disable_global_filter: bool | None = None,
    ) -> Self:
        """配置当前 CRUD 实例的行为。"""
        if error_policy is not None:
            self._error_policy = error_policy
        if disable_global_filter is not None:
            self._apply_global_filters = not disable_global_filter
        return self

    def create_instance(self, no_attach: bool = False) -> ModelTypeVar:
        """创建或返回当前绑定的模型实例。"""
        if no_attach:
            return cast(ModelTypeVar, self._model(**self._kwargs))
        if self.instance is None:
            self.instance = self._model(**self._kwargs)
        return cast(ModelTypeVar, self.instance)

    def add(
        self,
        instance: ModelTypeVar | None = None,
        **kwargs: Any,
    ) -> ModelTypeVar | None:
        """新增一条记录，并可选择在创建时更新字段。

        行为：
        - 当 instance 为 None 时，使用构造 CRUD 时传入的默认 kwargs 创建实例；
        - 否则，将给定 instance 合并到当前会话，并应用额外的字段更新。
        """
        try:
            if instance is None:
                instance = self.create_instance()

            self._ensure_sub_txn()

            need_merge = False
            try:
                if not (insp := sa_inspect(instance)):
                    raise ValueError()
                bound_sess = object_session(instance)
                need_merge = (not insp.transient) or (
                    bound_sess is not None and bound_sess is not self._session
                )
            except Exception:  # pylint: disable=broad-exception-caught
                need_merge = True

            session = self._require_session()
            target = (
                cast(ModelTypeVar, session.merge(instance)) if need_merge else instance
            )

            if kwargs:
                updated = self.update(target, **kwargs)
                if updated is not None:
                    target = updated

            session.add(target)
            # 立即 flush，以便为调用方提供主键等数据库生成字段
            session.flush()
            self._need_commit = True
            self._mark_dirty()
            return target
        except SQLAlchemyError as exc:
            self._on_sql_error(exc)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.error = exc
            self.status = SQLStatus.INTERNAL_ERR
        return None

    def add_many(
        self,
        instances: list[ModelTypeVar],
        **kwargs: Any,
    ) -> list[ModelTypeVar] | None:
        """批量新增多条记录，并可为每条记录应用相同的字段更新。"""
        try:
            if not instances:
                return []

            self._ensure_sub_txn()

            managed_instances: list[ModelTypeVar] = []
            for instance in instances:
                need_merge = False
                try:
                    if not (insp := sa_inspect(instance)):
                        raise ValueError()
                    bound_sess = object_session(instance)
                    need_merge = (not insp.transient) or (
                        bound_sess is not None and bound_sess is not self._session
                    )
                except Exception:  # pylint: disable=broad-exception-caught
                    need_merge = True

                session = self._require_session()
                target = (
                    cast(ModelTypeVar, session.merge(instance))
                    if need_merge
                    else instance
                )
                if kwargs:
                    updated = self.update(target, **kwargs)
                    if updated is not None:
                        managed_instances.append(updated)
                        continue
                managed_instances.append(target)

            session = self._require_session()
            session.add_all(managed_instances)
            session.flush()
            self._need_commit = True
            self._mark_dirty()
            return managed_instances
        except SQLAlchemyError as exc:
            self._on_sql_error(exc)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.error = exc
            self.status = SQLStatus.INTERNAL_ERR
        return None

    def query(
        self, *args, pure: bool = False, **kwargs
    ) -> CRUDQuery[ModelTypeVar, ModelTypeVar]:
        """构造带有默认过滤条件的查询对象。"""
        session = self._require_session()
        base_query = self._get_query_factory()(self._model, session)
        query = cast(
            Query, base_query.query if isinstance(base_query, CRUDQuery) else base_query
        )
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
        except SQLAlchemyError as exc:
            self._on_sql_error(exc)
            self._log(exc, self.status)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.error = exc
            self.status = SQLStatus.INTERNAL_ERR
            self._log(exc, self.status)
        return CRUDQuery(self, final_query)

    def first(
        self, query: CRUDQuery[ModelTypeVar, ModelTypeVar] | None = None
    ) -> ModelTypeVar | None:
        """执行查询并返回首条记录。"""
        if query is None:
            query = self.query()
        return query.first()

    def all(
        self, query: CRUDQuery[ModelTypeVar, ModelTypeVar] | None = None
    ) -> list[ModelTypeVar]:
        """执行查询并返回所有记录。"""
        if query is None:
            query = self.query()
        return query.all()

    def update(
        self, instance: ModelTypeVar | None = None, **kwargs
    ) -> ModelTypeVar | None:
        """更新单条记录的字段值。"""
        try:
            if instance is None:
                instance = self.query().first()

            if not instance:
                return None

            self._ensure_sub_txn()
            session = self._require_session()
            no_autoflush_cm = session.no_autoflush
            assert no_autoflush_cm is not None
            with no_autoflush_cm:
                for k, v in kwargs.items():
                    setattr(instance, k, v)
            self._need_commit = True
            self._mark_dirty()
            return instance
        except SQLAlchemyError as exc:
            self._on_sql_error(exc)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.error = exc
            self.status = SQLStatus.INTERNAL_ERR
        return None

    def delete(
        self,
        instance: ModelTypeVar | None = None,
        query: CRUDQuery[ModelTypeVar, ModelTypeVar] | None = None,
        all_records: bool = False,
        sync: _orm_types.SynchronizeSessionArgument = "fetch",
    ) -> bool:
        """删除单条或多条记录。"""
        try:
            session = self._require_session()
            if instance:
                self._ensure_sub_txn()
                session.delete(instance)
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
                    session.delete(first_inst)

            self._need_commit = True
            self._mark_dirty()
            return True
        except SQLAlchemyError as exc:
            self._on_sql_error(exc)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self.error = exc
            self.status = SQLStatus.INTERNAL_ERR
        return False

    def need_commit(self) -> None:
        """标记当前上下文在退出时需要提交事务。"""
        self._ensure_sub_txn()
        self._need_commit = True
        self._mark_dirty()

    def commit(self) -> None:
        """显式提交当前子事务或会话。"""
        try:
            session = self._require_session()
            if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                self._sub_txn.commit()
            else:
                session.commit()
            self._explicit_committed = True
            self._need_commit = False
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._logger("CRUD commit failed: %s", exc)
            if self._session is not None:
                self._session.rollback()

    def discard(self) -> None:
        """显式回滚当前事务，并标记上下文为已丢弃。

        - 不向外抛出异常，由调用方自行继续后续逻辑。
        - 仅通过 `_discarded` 标记告知 __exit__ 执行回滚。
        """
        try:
            session = self._require_session()
            if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                self._sub_txn.rollback()
            else:
                session.rollback()
        finally:
            self._need_commit = False
            self._discarded = True

    def _log(self, error: Exception, status: SQLStatus = SQLStatus.INTERNAL_ERR):
        """记录一条与当前模型相关的错误日志。"""
        model_name = getattr(self._model, "__name__", str(self._model))
        self._logger(
            "CRUD[%s]: <catch: %s> <except: (%s)>",
            model_name,
            error,
            status,
        )

    def __exit__(
        self, exc_type, exc_val, exc_tb
    ) -> None:  # pylint: disable=too-many-branches,too-many-nested-blocks
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
                    self._logger(
                        "CRUD[%s]: <catch: %s> <except: (%s: %s)>",
                        model_name,
                        self.error,
                        exc_type,
                        exc_val,
                    )
                try:
                    if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                        self._sub_txn.rollback()
                except Exception:  # pylint: disable=broad-exception-caught
                    # 子事务回滚失败时记录错误并继续走顶层回滚逻辑。
                    self._logger("CRUD sub-txn rollback failed", exc_info=True)
                self._need_commit = False
            elif self._need_commit and not self._explicit_committed:
                try:
                    if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                        self._sub_txn.commit()
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    self._logger("CRUD sub-txn commit failed: %s", exc)
                    raise

            # 基于通用事务状态机调整深度，并在最外层执行提交/回滚
            if self._session is not None:
                session = self._session
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
                        except (
                            Exception
                        ) as exc:  # pylint: disable=broad-exception-caught
                            self._logger(
                                "CRUD commit/rollback failed: %s",
                                exc,
                            )
                            try:
                                session.rollback()
                            except Exception:  # pragma: no cover - 防御性回滚
                                pass
                            raise
        finally:
            # Session 的生命周期由外部（如应用框架）负责管理。
            self._session = None

    def _ensure_sub_txn(self) -> None:
        """确保当前存在一个活跃的子事务。"""
        if not (self._sub_txn and self._sub_txn.is_active):
            try:
                session = self._require_session()
                self._sub_txn = session.begin_nested()
            except Exception:  # pylint: disable=broad-exception-caught
                self._sub_txn = None

    def _mark_dirty(self) -> None:
        # 当前上下文事务由通用状态机管理，保留占位以便未来扩展。
        return

    def _on_sql_error(self, e: Exception) -> None:
        """处理 SQLAlchemyError，并根据策略决定是否抛出。"""
        self.error = e
        self.status = SQLStatus.SQL_ERR
        try:
            session = self._require_session()
            if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                self._sub_txn.rollback()
            else:
                session.rollback()
        except Exception:  # pylint: disable=broad-exception-caught
            self._logger("CRUD SQL rollback failed", exc_info=True)
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
            provider = cls._get_session_provider()
            return provider()

        return _txn_transaction(
            session_factory,
            join=join,
            nested=nested,
            error_policy=resolved_policy,
        )
