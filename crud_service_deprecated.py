from functools import wraps

# mypy: ignore-errors
from typing import Generic, Optional, Type, TypeVar, overload

from flask import g, has_request_context
from flask_sqlalchemy.model import Model
from flask_sqlalchemy.query import Query
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import object_session
from sqlalchemy.sql import _orm_types

from app.core.database import db
from app.utils.logger import Log
from app.utils.structs import SQLStatus

ModelTypeVar = TypeVar("ModelTypeVar", bound=Model)


class CRUD(Generic[ModelTypeVar]):
    """CRUD

    具有上下文管理器的事务管理，支持全局事务共享，简化数据库操作。

    主要特性：
    - 上下文管理器自动提交/回滚
    - 全局事务共享（同一请求内所有CRUD实例共享事务）
    - 统一的错误处理和状态管理
    - 支持复杂查询和批量操作
    - 全局与实例默认条件：可通过类级全局过滤 + 实例级默认kwargs，自动应用到查询

    Args:
        model: 要操作的 SQLAlchemy 模型类。
        **kwargs: 实例默认条件（将自动用于 filter_by），同时用于 create_instance() 的默认属性。

    使用示例：
    ```python
    # 需要使用 with 上下文管理器以在做出更改时提交

    # 基础查询
    students = CRUD(Student).all()
    user = CRUD(User, id="123").first()

    # 复杂查询
    query_students = CRUD(Student, status="normal")
    query = query_students.query(Student.age >= 18, Student.class_id == "class-123")
    active_students = query_students.all(query)

    # 创建单条记录
    with CRUD(Student) as crud:
        student = crud.add(name="张三", age=16, class_id="class-123")

    # 批量创建记录
    with CRUD(Student) as crud:
        students = crud.add([
            Student(name="学生1", age=16),
            Student(name="学生2", age=17)
        ])

    # 更新记录
    with CRUD(User, id="123") as crud:
        if user := crud.first():
            crud.update(user, name="新名字", age=25)

    # 删除记录
    with CRUD(Student, id="456") as crud:
        crud.delete()

    # 批量操作（同一事务中）
    with CRUD(Student) as crud:
        crud.add(name="学生1", class_id="class-1")
        crud.add(name="学生2", class_id="class-1")

    # 事务共享示例
    with CRUD(...) as crud:
        student = crud.add(name="张三", age=16, class_id="class-123")
        CRUD(Course).add(id=student.id)

    # 绕过底层过滤（干净起点）
    q = CRUD(User).query(pure=True, User.age >= 18)

    # 临时关闭全局过滤并查询
    crud = CRUD(User)
    rows = crud.config(disable_global_filter=True).all(crud.query(User.age >= 18))
    crud.config(disable_global_filter=False)
    ```
    """

    _global_filter_conditions: tuple[list, dict] = ([], {})
    _CTX_KEY = "_crud_v3_ctx"
    session = db.session

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

    def query(self, *args, pure=False, **kwargs) -> Query:
        """构建查询

        Args:
            *args: 通过 `filter` 叠加的 SQLAlchemy 表达式。
            pure: True 时跳过全局过滤与实例默认条件，直接从“干净起点”构建。
            **kwargs: 通过 `filter_by` 叠加的键值条件。

        Returns:
            SQLAlchemy Query 对象。
        """
        # 每次从“干净”的 self._model.query 开始
        query = self._model.query
        if not pure:
            if self._instance_default_kwargs:
                query = query.filter_by(**self._instance_default_kwargs)
            if self._apply_global_filters:
                if self._base_filter_exprs:
                    query = query.filter(*self._base_filter_exprs)
                if self._base_filter_kwargs:
                    query = query.filter_by(**self._base_filter_kwargs)
        try:
            query = query.filter(*args).filter_by(**kwargs)
            return query
        except SQLAlchemyError as e:
            self._on_sql_error(e)
            self._log(e, self.status)
        except Exception as e:
            self.error = e
            self.status = SQLStatus.INTERNAL_ERR
            self._log(e, self.status)
        return query

    def first(self, query: Query | None = None) -> ModelTypeVar | None:
        """获取第一条记录。

        Args:
            query: 预构建的查询，可选。

        Returns:
            第一条记录或 None。
        """
        if query is None:
            query = self.query()
        return query.first()

    def all(self, query: Query | None = None) -> list[ModelTypeVar]:
        """获取所有记录。

        Args:
            query: 预构建的查询，可选。

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
        query: Query | None = None,
        all_records=False,
        sync: _orm_types.SynchronizeSessionArgument = "fetch",
    ) -> bool:
        """删除记录。

        - 若提供 `instance`，直接删除该实例。
        - 否则按查询条件删除第一条或全部匹配记录。

        Args:
            instance: 要删除的实例，可选。
            query: 预构建的查询，可选。
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
            Log.error(f"CRUD commit failed: {e}")
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
        Log.error(
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
                    Log.error(
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
                    Log.error(f"CRUD commit failed: {e}")
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
                        Log.error(f"CRUD root commit failed: {e}")
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
                ctx["root_txn"] = db.session.begin()
            except Exception:
                ctx["root_txn"] = None
        ctx["depth"] = ctx.get("depth", 0) + 1

    @classmethod
    def transaction(cls):
        """函数级事务装饰器。

        作用：
        - 在请求上下文中：确保开启请求级根事务（共享），并为函数体开启子事务（SAVEPOINT）。
          函数执行完毕后：成功则提交子事务；异常则回滚子事务并标记错误。
          最后当作用域深度归零时，统一提交/回滚根事务并清理上下文与会话。
        - 在无请求上下文中：独立开启根事务与子事务，函数成功则提交，失败则回滚，最后关闭会话。
        """

        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                if has_request_context():
                    # 请求上下文：仅确保根事务，子事务由 CRUD 写操作懒开启
                    cls._ensure_root_txn_cls()
                    try:
                        result = func(*args, **kwargs)
                        return result
                    except Exception:
                        # 标记错误并回滚会话（包括可能存在的子事务）
                        ctx = cls._get_request_ctx_cls(create=False)
                        if ctx is not None:
                            ctx["error"] = True
                        try:
                            db.session.rollback()
                        except Exception:
                            pass
                        raise
                    finally:
                        # 作用域计数与最终决策
                        ctx = cls._get_request_ctx_cls(create=False)
                        if ctx is not None:
                            ctx["depth"] = max(0, ctx.get("depth", 0) - 1)
                            if ctx["depth"] == 0:
                                try:
                                    if ctx.get("error"):
                                        db.session.rollback()
                                    else:
                                        db.session.commit()
                                except Exception as e2:
                                    Log.error(f"CRUD root commit failed: {e2}")
                                    db.session.rollback()
                                    # 不在此处抛出，保持装饰器对外语义
                                finally:
                                    try:
                                        delattr(g, cls._CTX_KEY)
                                    except Exception:
                                        pass
                else:
                    # 非请求上下文：独立事务，子事务按需由 CRUD 写操作懒开启
                    sub_txn = None
                    try:
                        sub_txn = db.session.begin_nested()
                        result = func(*args, **kwargs)
                        try:
                            sub_txn.commit()
                        except Exception:
                            pass
                        try:
                            db.session.commit()
                        except Exception:
                            db.session.rollback()
                            raise
                        return result
                    except Exception:
                        try:
                            if sub_txn is not None:
                                sub_txn.rollback()
                        except Exception:
                            pass
                        try:
                            db.session.rollback()
                        except Exception:
                            pass
                        raise
                    finally:
                        try:
                            db.session.close()
                        except Exception:
                            pass
                        try:
                            db.session.remove()
                        except Exception:
                            pass

            return wrapper

        return decorator
