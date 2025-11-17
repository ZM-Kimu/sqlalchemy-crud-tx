from functools import wraps
from typing import Any, Callable, Generic, Iterator, Optional, Type, TypeVar, cast, overload

import logging
from flask import g, has_request_context
from flask_sqlalchemy.model import Model
from flask_sqlalchemy.query import Query
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import ScalarResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import object_session
from sqlalchemy.sql import _orm_types


class SQLStatus:
    """SQLalchemy返回的状态枚举"""

    OK = 0
    SQL_ERR = 1
    INTERNAL_ERR = 2

    NOT_FOUND = 5


ModelTypeVar = TypeVar("ModelTypeVar", bound=Model)
ResultTypeVar = TypeVar("ResultTypeVar", covariant=True)
EntityTypeVar = TypeVar("EntityTypeVar", covariant=True)


ErrorLogger = Callable[[str], None]
_error_logger: ErrorLogger = logging.getLogger("CRUD").error


def _get_session_for_cls(crud_cls: Type["CRUD"]) -> Any:
    """根据 CRUD 类获取当前配置的会话对象。

    依赖外部通过 configure_crud 或 CRUD.configure 预先设置 session。
    """
    session = getattr(crud_cls, "session", None)
    if session is None:
        raise RuntimeError(
            "CRUD session is not configured. "
            "Please call configure_crud(session=...) or CRUD.configure(session=...)."
        )
    return session


class _TransactionScope:
    """简化 transaction 装饰器内部的事务管理。"""

    __slots__ = ("_crud_cls", "_is_request", "_sub_txn")

    def __init__(self, crud_cls: Type["CRUD"], is_request: bool) -> None:
        self._crud_cls = crud_cls
        self._is_request = is_request
        self._sub_txn = None

    def __enter__(self):
        if self._is_request:
            self._crud_cls._ensure_root_txn_cls()
            return self
        try:
            session = _get_session_for_cls(self._crud_cls)
            self._sub_txn = session.begin_nested()
        except Exception:
            self._sub_txn = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._is_request:
            try:
                session = _get_session_for_cls(self._crud_cls)
            except Exception:
                session = None
            ctx = self._crud_cls._get_request_ctx_cls(create=False)
            if exc_type is not None and ctx is not None:
                ctx["error"] = True
            if exc_type is not None and session is not None:
                try:
                    session.rollback()
                except Exception:
                    pass
            if ctx is not None:
                ctx["depth"] = max(0, ctx.get("depth", 0) - 1)
                if ctx["depth"] == 0:
                    try:
                        if session is not None:
                            if ctx.get("error"):
                                session.rollback()
                            else:
                                session.commit()
                    except Exception as exc:
                        _error_logger(f"CRUD root commit failed: {exc}")
                        if session is not None:
                            try:
                                session.rollback()
                            except Exception:
                                pass
                        ctx["error"] = True
                    finally:
                        try:
                            delattr(g, self._crud_cls._CTX_KEY)
                        except Exception:
                            pass
            return False

        success = exc_type is None
        try:
            session = _get_session_for_cls(self._crud_cls)
        except Exception:
            session = None
        try:
            if success:
                if self._sub_txn is not None and getattr(
                    self._sub_txn, "is_active", False
                ):
                    try:
                        self._sub_txn.commit()
                    except Exception:
                        pass
                if session is not None:
                    session.commit()
            else:
                if self._sub_txn is not None and getattr(
                    self._sub_txn, "is_active", False
                ):
                    try:
                        self._sub_txn.rollback()
                    except Exception:
                        pass
                if session is not None:
                    session.rollback()
        except Exception:
            if session is not None:
                try:
                    session.rollback()
                except Exception:
                    pass
            if success:
                raise
        finally:
            if session is not None:
                for name in ("close", "remove"):
                    func = getattr(session, name, None)
                    if callable(func):
                        try:
                            func()
                        except Exception:
                            pass
        return False


class CRUDQuery(Generic[ModelTypeVar, ResultTypeVar]):
    """Query 包装器

    - 保留 SQLAlchemy 原生 Query 功能，同时增加类型提示与链式体验
    - 通过 __getattr__ 委托未覆盖的方法，确保与既有代码兼容
    - 终结方法（first/all/...）直接调用底层 Query，实现一致的行为
    """

    __slots__ = ("_crud", "_query")

    def __init__(self, crud: "CRUD[ModelTypeVar]", query: Query) -> None:
        self._crud = crud
        self._query = query

    @property
    def query(self) -> Query:
        """返回底层 SQLAlchemy Query"""
        return self._query

    def _wrap(self, query: Query) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return cast(
            "CRUDQuery[ModelTypeVar, ResultTypeVar]", CRUDQuery(self._crud, query)
        )

    def join(self, *args, **kwargs) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.join(*args, **kwargs))

    def outerjoin(self, *args, **kwargs) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.outerjoin(*args, **kwargs))

    def filter(self, *criterion) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.filter(*criterion))

    def filter_by(self, **kwargs) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.filter_by(**kwargs))

    def distinct(self, *criterion) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.distinct(*criterion))

    def options(self, *options) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.options(*options))

    @overload
    def with_entities(
        self, entity: EntityTypeVar, /
    ) -> "CRUDQuery[ModelTypeVar, EntityTypeVar]": ...

    @overload
    def with_entities(
        self, __entity: Any, __other: Any, *entities: Any
    ) -> "CRUDQuery[ModelTypeVar, tuple[Any, ...]]": ...

    def with_entities(self, *entities: Any):
        new_query = self._query.with_entities(*entities)
        wrapper: "CRUDQuery[ModelTypeVar, Any]" = CRUDQuery(self._crud, new_query)
        if len(entities) == 1:
            return cast("CRUDQuery[ModelTypeVar, EntityTypeVar]", wrapper)
        return cast("CRUDQuery[ModelTypeVar, tuple[Any, ...]]", wrapper)

    def order_by(self, *clauses) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.order_by(*clauses))

    def group_by(self, *clauses) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.group_by(*clauses))

    def having(self, *criterion) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.having(*criterion))

    def limit(self, limit: int | None) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.limit(limit))

    def offset(self, offset: int | None) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.offset(offset))

    def select_from(self, *entities) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.select_from(*entities))

    def execution_options(
        self, *args, **kwargs
    ) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.execution_options(*args, **kwargs))

    def enable_eagerloads(
        self, value: bool
    ) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.enable_eagerloads(value))

    def all(self) -> list[ResultTypeVar]:
        return self._query.all()

    def first(self) -> ResultTypeVar | None:
        return self._query.first()

    def one(self) -> ResultTypeVar:
        return self._query.one()

    def one_or_none(self) -> ResultTypeVar | None:
        return self._query.one_or_none()

    def scalar(self) -> Optional[ResultTypeVar]:
        result = self._query.scalar()
        return cast(Optional[ResultTypeVar], result)

    def scalar_one(self) -> ResultTypeVar:
        return cast(ResultTypeVar, self._query.scalar_one())

    def scalars(self) -> ScalarResult[ResultTypeVar]:
        return cast(ScalarResult[ResultTypeVar], self._query.scalars())

    def count(self) -> int:
        return self._query.count()

    def paginate(self, *args, **kwargs):
        return self._query.paginate(*args, **kwargs)

    def raw(self) -> Query:
        return self._query

    @property
    def session(self):
        return self._query.session

    def __iter__(self) -> Iterator[ResultTypeVar]:
        return iter(self._query)

    def __getitem__(self, item):
        return self._query[item]

    def __getattr__(self, item):
        attr = getattr(self._query, item)
        if callable(attr):

            @wraps(attr)
            def wrapper(*args, **kwargs):
                result = attr(*args, **kwargs)
                if isinstance(result, Query):
                    return CRUDQuery(self._crud, result)
                return result

            return wrapper
        return attr

    def __repr__(self) -> str:
        return f"CRUDQuery({self._query!r})"


class CRUD(Generic[ModelTypeVar]):
    _global_filter_conditions: tuple[list, dict] = ([], {})
    _CTX_KEY = "_crud_v3_ctx"
    # 由外部通过 configure_crud / CRUD.configure 注册的会话对象
    session: Any | None = None

    @classmethod
    def register_global_filters(cls, *base_exprs, **base_kwargs) -> None:
        """为所有模型注册全局基础过滤。

        Args:
            *base_exprs: 需要通过 filter 应用的表达式（SQLAlchemy 二元表达式）。
            **base_kwargs: 需要通过 filter_by 应用的键值条件。
        """
        cls._global_filter_conditions = (list(base_exprs) or []), (base_kwargs or {})

    def __init__(self, model: Type[ModelTypeVar], **kwargs) -> None:
        """初始化 CRUD 实例。

        - 实例默认条件由 `**kwargs` 指定，默认通过 filter_by 自动应用于查询；同时也作为 create_instance() 的默认字段。

        Args:
            model: SQLAlchemy 模型类。
            **kwargs: 实例默认条件（filter_by），与实例默认属性（创建时）。
        """
        self._txn = None
        self._model = model
        self._kwargs = kwargs

        self.instance: ModelTypeVar | None = None
        self._base_filter_exprs: list = list(self._global_filter_conditions[0])
        self._base_filter_kwargs: dict = dict(self._global_filter_conditions[1])
        self._instance_default_kwargs: dict = dict(kwargs)

        self.error: Exception | None = None
        self.status: SQLStatus = SQLStatus.OK

        self._need_commit = False
        self._raise_on_error = False
        self._apply_global_filters = True
        self._sub_txn = None
        self._explicit_committed = False
        self._discarded = False

    def __enter__(self) -> "CRUD[ModelTypeVar]":
        """进入上下文管理器。"""
        # 确保请求级根事务存在（请求内共享）
        self._ensure_root_txn()
        self._explicit_committed = False
        self._discarded = False
        return self

    @classmethod
    def configure(
        cls,
        *,
        session: Any | None = None,
        error_logger: ErrorLogger | None = None,
    ) -> None:
        """配置 CRUD 所依赖的会话与日志函数（类级别）。

        一般推荐在应用初始化阶段调用一次，例如：

        ```python
        from app.core.database import db
        from app.utils.logger import Log
        from app.services.crud_service import CRUD

        CRUD.configure(session=db.session, error_logger=Log.error)
        ```
        """
        if session is not None:
            cls.session = session
        if error_logger is not None:
            global _error_logger
            _error_logger = error_logger

    def config(
        self,
        raise_on_error: bool | None = None,
        disable_global_filter: bool | None = None,
    ) -> "CRUD[ModelTypeVar]":
        """配置 CRUD 行为。

        Args:
            raise_on_error: 是否在内部捕获错误时抛出异常（默认 False）。
            disable_global_filter: True 则临时禁用全局过滤。

        Returns:
            self（便于链式调用）。
        """
        if raise_on_error is not None:
            self._raise_on_error = raise_on_error

        if disable_global_filter is not None:
            self._apply_global_filters = not disable_global_filter
        return self

    def create_instance(self, no_attach: bool = False) -> ModelTypeVar:
        """创建模型实例。

        - 若 `no_attach=True`，仅构造实例而不附加到当前类的实例内。

        Args:
            no_attach: 是否不附加到当前实例内。

        Returns:
            创建的模型实例。
        """
        if no_attach:
            return self._model(**self._kwargs)
        if self.instance is None:
            self.instance = self._model(**self._kwargs)
        return self.instance

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
        """添加记录。

        - 当不给定 `instances` 时，将使用 `create_instance()` 创建单个实例。
        - 若传入实例列表，会逐个更新属性（`**kwargs`）并统一 add_all。

        Args:
            instances: 预创建的单个实例或实例列表；不提供时将使用 `create_instance()`。
            **kwargs: 需批量设置到实例上的属性。

        Returns:
            创建的实例或实例列表；失败时返回 None。
        """
        try:
            instances = instances or self.create_instance()
            if not isinstance(instances, list):
                instances = [instances]

            # 写操作：懒开启子事务
            self._ensure_sub_txn()

            managed_instances: list[ModelTypeVar] = []
            for instance in instances:
                # 仅在实例不是瞬态或绑定到其他会话时进行 merge
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

                target = self.session.merge(instance) if need_merge else instance
                if updated := self.update(target, **kwargs):
                    managed_instances.append(updated)
                else:
                    managed_instances.append(target)

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
        self, *args, pure=False, **kwargs
    ) -> CRUDQuery[ModelTypeVar, ModelTypeVar]:
        """������ѯ

        Args:
            *args: ͨ�� `filter` ���ӵ� SQLAlchemy ����ʽ��
            pure: True ʱ����ȫ�ֹ�����ʵ��Ĭ��������ֱ�Ӵӡ��ɾ���㡱������
            **kwargs: ͨ�� `filter_by` ���ӵļ�ֵ������

        Returns:
            CRUDQuery ����, ����֧��链式���ɣ�
        """
        # ÿ�δӡ��ɾ����� self._model.query ��ʼ
        query = self._model.query
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
        """获取第一条记录。

        Args:
            query: 预构建的 CRUDQuery，可选。

        Returns:
            第一条记录或 None。
        """
        if query is None:
            query = self.query()
        return query.first()

    def all(
        self, query: CRUDQuery[ModelTypeVar, ModelTypeVar] | None = None
    ) -> list[ModelTypeVar]:
        """获取所有记录。

        Args:
            query: 预构建的 CRUDQuery，可选。

        Returns:
            记录列表。
        """
        if query is None:
            query = self.query()
        return query.all()

    def update(
        self, instance: ModelTypeVar | None = None, **kwargs
    ) -> ModelTypeVar | None:
        """更新记录。若未提供 `instance`，则按当前默认查询条件查找第一条并更新。

        Args:
            instance: 要更新的实例，可选。
            **kwargs: 要更新的属性键值。

        Returns:
            更新后的实例，若不存在返回 None。
        """
        try:
            if instance is None:
                instance = self.query().first()

            if not instance:
                return None

            # 写操作：懒开启子事务，并避免中途自动 flush
            self._ensure_sub_txn()
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
        all_records=False,
        sync: _orm_types.SynchronizeSessionArgument = "fetch",
    ) -> bool:
        """删除记录。

        - 若提供 `instance`，直接删除该实例。
        - 否则按查询条件删除第一条或全部匹配记录。

        Args:
            instance: 要删除的实例，可选。
            query: 预构建的 CRUDQuery，可选。
            all_records: True 时删除所有匹配记录，否则仅删除第一条。

        Returns:
            删除是否成功。
        """
        try:
            if instance:
                # 写操作：懒开启子事务
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
        """标记该事务的更改需要提交。

        - 当手动修改实例属性后调用此方法以标记需要提交。
        """
        # 写操作：懒开启子事务
        self._ensure_sub_txn()
        self._need_commit = True
        self._mark_dirty()

    def commit(self) -> None:
        """立即提交更改。

        - 失败会自动回滚并记录日志。
        - 若存在子事务，优先提交子事务；否则提交会话。
        - 显式提交后，__exit__ 不再重复提交。
        """
        try:
            if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                self._sub_txn.commit()
            else:
                # 仅当不在请求级根事务内时才直接提交会话
                ctx = self._get_request_ctx(create=False)
                if not (has_request_context() and ctx and ctx.get("root_txn")):
                    self.session.commit()
            self._explicit_committed = True
            self._need_commit = False
        except Exception as e:
            _error_logger(f"CRUD commit failed: {e}")
            self.session.rollback()

    def discard(self) -> None:
        """放弃更改并回滚。"""
        try:
            if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                self._sub_txn.rollback()
            else:
                self.session.rollback()
        finally:
            self.error = AssertionError("User called rollback.")
            self._need_commit = False
            self._discarded = True

    def _log(self, error: Exception, status=SQLStatus.INTERNAL_ERR):
        """统一错误日志格式。"""
        model_name = getattr(self._model, "__name__", str(self._model))
        depth = None
        try:
            ctx = self._get_request_ctx(create=False)
            depth = ctx.get("depth") if ctx else None
        except Exception:
            pass
        _error_logger(
            f"CRUD[{model_name}]: <catch: {error}> <except: ({status.value})> <depth: {depth}>"
        )

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """退出上下文管理器，自动处理提交/回滚。"""
        if self.error and self._raise_on_error:
            raise self.error
        try:
            has_exc = bool(exc_type or exc_val or exc_tb)
            should_rollback = has_exc or self.error is not None or self._discarded

            if should_rollback:
                if has_exc or self.error:
                    model_name = getattr(self._model, "__name__", str(self._model))
                    depth = None
                    try:
                        ctx_tmp = self._get_request_ctx(create=False)
                        depth = ctx_tmp.get("depth") if ctx_tmp else None
                    except Exception:
                        pass
                    _error_logger(
                        f"CRUD[{model_name}]: <catch: {self.error}> <except: ({exc_type}: {exc_val})> <depth: {depth}>"
                    )
                try:
                    if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                        self._sub_txn.rollback()
                    else:
                        self.session.rollback()
                except Exception:
                    pass
                self._need_commit = False
            elif self._need_commit and not self._explicit_committed:
                try:
                    if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                        self._sub_txn.commit()
                    else:
                        # 非请求级根事务环境才直接提交
                        ctx_tmp = self._get_request_ctx(create=False)
                        if ctx_tmp is None or not ctx_tmp.get("root_txn"):
                            self.session.commit()
                except Exception as e:
                    _error_logger(f"CRUD commit failed: {e}")
                    self.session.rollback()
                    if self._raise_on_error:
                        raise e

            # 若是请求级事务，引用计数到 0 时决定提交/回滚外层事务
            ctx = self._get_request_ctx(create=False)
            if ctx is not None:
                ctx["depth"] = max(0, ctx.get("depth", 0) - 1)
                if ctx["depth"] == 0:
                    try:
                        if ctx.get("error"):
                            self.session.rollback()
                        else:
                            self.session.commit()
                    except Exception as e:
                        _error_logger(f"CRUD root commit failed: {e}")
                        self.session.rollback()
                        if self._raise_on_error:
                            raise e
                    finally:
                        try:
                            delattr(g, self._CTX_KEY)
                        except Exception:
                            pass
        finally:
            if not has_request_context():
                try:
                    self.session.close()
                except Exception:
                    pass
                try:
                    self.session.remove()
                except Exception:
                    pass

    def _get_request_ctx(self, create: bool = True):
        if not has_request_context():
            return None
        ctx = getattr(g, self._CTX_KEY, None)
        if ctx is None and create:
            ctx = {"root_txn": None, "depth": 0, "error": False, "dirty": False}
            setattr(g, self._CTX_KEY, ctx)
        return ctx

    def _ensure_root_txn(self) -> None:
        ctx = self._get_request_ctx(create=True)
        if ctx is None:
            return
        if ctx.get("root_txn") is None or not getattr(
            ctx.get("root_txn"), "is_active", True
        ):
            try:
                ctx["root_txn"] = self.session.begin()
            except Exception:
                # 回退到隐式事务
                ctx["root_txn"] = None
        ctx["depth"] = ctx.get("depth", 0) + 1

    def _ensure_sub_txn(self) -> None:
        if not (self._sub_txn and self._sub_txn.is_active):
            try:
                self._sub_txn = self.session.begin_nested()
            except Exception:
                self._sub_txn = None

    def _mark_dirty(self) -> None:
        ctx = self._get_request_ctx(create=False)
        if ctx is not None:
            ctx["dirty"] = True

    def _on_sql_error(self, e: Exception) -> None:
        self.error = e
        self.status = SQLStatus.SQL_ERR
        # 标记请求级错误并尽快回滚子事务/会话
        ctx = self._get_request_ctx(create=False)
        if ctx is not None:
            ctx["error"] = True
        try:
            if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                self._sub_txn.rollback()
            else:
                self.session.rollback()
        except Exception:
            pass
        self._need_commit = False

    @classmethod
    def _get_request_ctx_cls(cls, create: bool = True):
        if not has_request_context():
            return None
        ctx = getattr(g, cls._CTX_KEY, None)
        if ctx is None and create:
            ctx = {"root_txn": None, "depth": 0, "error": False, "dirty": False}
            setattr(g, cls._CTX_KEY, ctx)
        return ctx

    @classmethod
    def _ensure_root_txn_cls(cls) -> None:
        ctx = cls._get_request_ctx_cls(create=True)
        if ctx is None:
            return
        if ctx.get("root_txn") is None or not getattr(
            ctx.get("root_txn"), "is_active", True
        ):
            try:
                session = _get_session_for_cls(cls)
                ctx["root_txn"] = session.begin()
            except Exception:
                ctx["root_txn"] = None
        ctx["depth"] = ctx.get("depth", 0) + 1

    @classmethod
    def transaction(cls):
        """函数级事务装饰器。

        - 请求上下文：复用请求根事务，退出时统一提交/回滚。
        - 非请求上下文：独立开启事务，函数结束后提交或回滚并清理会话。
        """

        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                scope = _TransactionScope(cls, has_request_context())
                with scope:
                    return func(*args, **kwargs)

            return wrapper

        return decorator
