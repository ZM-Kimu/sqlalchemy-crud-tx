"""CRUD helper utilities and transaction integration."""

from __future__ import annotations

import logging
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    ParamSpec,
    Self,
    TypeAlias,
    TypeVar,
    cast,
)

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Query, SessionTransaction, object_session
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
from .types import ErrorLogger, ORMModel, QueryBuilder, SessionLike, SessionProvider

P = ParamSpec("P")
R = TypeVar("R")

ModelTypeVar = TypeVar("ModelTypeVar", bound=ORMModel)
ResultTypeVar_co = TypeVar("ResultTypeVar_co", covariant=True)

_DEFAULT_LOGGER: ErrorLogger = logging.getLogger("CRUD").error


def _default_query_builder(
    model: type[ModelTypeVar], session: SessionLike, crud: "CRUD[ModelTypeVar]"
) -> CRUDQuery[ModelTypeVar, ModelTypeVar]:
    """Build a query using plain SQLAlchemy Session (no Flask-SQLAlchemy dependency)."""
    sa_query = session.query(model)
    return CRUDQuery(crud, cast(Query, sa_query))


class SessionProxy:
    """Session facade exposed to callers.

    - Delegates most methods directly to the underlying Session.
    - commit/rollback calls are redirected to CRUD.commit / CRUD.discard
      to avoid bypassing the CRUD transaction state machine.
    """

    __slots__ = ("_crud", "_session")

    def __init__(self, crud: "CRUD[Any]", session: SessionLike) -> None:
        self._crud = crud
        self._session = session

    def commit(self) -> None:
        """Redirect to CRUD.commit with a warning."""
        self._crud.logger(
            "CRUD.session.commit() is redirected to CRUD.commit(); "
            "consider calling CRUD.commit() explicitly.",
        )
        self._crud.commit()

    def rollback(self) -> None:
        """Redirect to CRUD.discard with a warning."""
        self._crud.logger(
            "CRUD.session.rollback() is redirected to CRUD.discard(); "
            "consider calling CRUD.discard() explicitly.",
        )
        self._crud.discard()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


if TYPE_CHECKING:
    SessionViewType: TypeAlias = SessionLike
else:
    SessionViewType = SessionProxy


class CRUD(Generic[ModelTypeVar]):
    """Generic CRUD wrapper.

    - Uses a context manager for commit / rollback.
    - Provides unified error state management via ``SQLStatus``.
    - Supports global and per-instance default filter conditions.
    """

    _global_filter_conditions: tuple[list, dict] = ([], {})
    _session_provider: SessionProvider | None = None
    _query_builder: QueryBuilder | None = None
    _default_error_policy: ErrorPolicy = "raise"
    _logger: ErrorLogger = _DEFAULT_LOGGER

    @classmethod
    def register_global_filters(cls, *base_exprs, **base_kwargs) -> None:
        """Register global base filters applied to all models.

        Args:
            *base_exprs: Positional filter expressions passed to ``Query.filter``.
            **base_kwargs: Keyword-style filters passed to ``Query.filter_by``.
        """
        cls._global_filter_conditions = (list(base_exprs) or []), (base_kwargs or {})

    def __init__(self, model: type[ModelTypeVar], **kwargs: Any) -> None:
        """Initialize a CRUD instance bound to a model.

        Args:
            model: ORM model class this CRUD instance operates on.
            **kwargs: Default filter/initialization kwargs bound to this
                instance (used by ``query()`` and ``create_instance()``).
        """
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
        self._joined_existing = False
        self._nested_txn: SessionTransaction | None = None
        self._explicit_committed = False
        self._discarded = False
        self._session: SessionLike | None = None

    def resolve_error_policy(self) -> ErrorPolicy:
        """Resolve the effective ``error_policy`` for this CRUD instance.

        Priority order:
        1. Error policy from the current transaction decorator context;
        2. Per-instance configuration (``config``);
        3. Class-level default configuration (``_default_error_policy``).
        """
        from_ctx = get_current_error_policy()
        if from_ctx is not None:
            return from_ctx
        if self._error_policy is not None:
            return self._error_policy
        return self._default_error_policy

    def __enter__(self) -> Self:
        """Enter the context manager and join or create a transaction scope."""
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
        self._joined_existing = joined_existing
        self._explicit_committed = False
        self._discarded = False
        return self

    @classmethod
    def configure(
        cls,
        *,
        session_provider: SessionProvider | None = None,
        query_builder: QueryBuilder | None = None,
        logger: ErrorLogger | None = None,
        error_policy: ErrorPolicy | None = None,
    ) -> None:
        """Configure session provider, query builder, logger and defaults.

        This is a class-level configuration and must be called before using
        ``CRUD(...)``.

        Args:
            session_provider: Callable that returns a ``SessionLike``. This is
                the preferred way to integrate with ``sessionmaker`` or custom
                session management.
            query_builder: Optional builder to construct a ``CRUDQuery`` (or
                Query-like object) from ``(model, session)``. When omitted the
                default builder uses ``session.query(model)``.
            logger: Optional logger callable used by CRUD to report internal
                errors.
            error_policy: Default error policy (``\"raise\"`` or ``\"status\"``)
                applied when no transaction-scoped policy or per-instance
                override is present.
        Raises:
            ValueError: If ``session_provider`` is not provided.
        """
        if session_provider is None:
            raise ValueError(
                "session_provider is required for CRUD.configure; "
                "pass a callable that returns an active Session."
            )

        cls._session_provider = session_provider

        if query_builder is not None:
            cls._query_builder = query_builder

        if logger is not None:
            cls._logger = logger
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

    def _get_query_builder(self) -> QueryBuilder:
        if self._query_builder is not None:
            return self._query_builder
        if type(self)._query_builder is not None:
            return cast(QueryBuilder, type(self)._query_builder)
        return lambda model, session: _default_query_builder(model, session, self)

    def _require_session(self) -> SessionLike:
        if self._session is None:
            raise RuntimeError("CRUD session is not bound to current context.")
        return self._session

    @property
    def session(self) -> SessionViewType:
        """Return a controlled Session view.

        The returned object exposes most Session APIs but redirects
        ``commit``/``rollback`` to ``CRUD.commit``/``CRUD.discard``.
        It should only be used inside a ``with CRUD(...)`` context.
        """
        session = self._require_session()
        return cast(SessionViewType, SessionProxy(self, session))

    def config(
        self,
        error_policy: ErrorPolicy | None = None,
        disable_global_filter: bool | None = None,
    ) -> Self:
        """Configure behaviour for this CRUD instance.

        Args:
            error_policy: Override the effective error policy for this instance.
                When ``None``, the class-level default or transaction-scoped
                setting is used.
            disable_global_filter: When ``True``, skip any global filters
                registered via ``register_global_filters`` for this instance's
                queries. When ``False`` or ``None``, global filters remain
                enabled.
        Returns:
            The same CRUD instance, to allow fluent-style configuration.
        """
        if error_policy is not None:
            self._error_policy = error_policy
        if disable_global_filter is not None:
            self._apply_global_filters = not disable_global_filter
        return self

    def create_instance(self, fresh: bool = False) -> ModelTypeVar:
        """Create or return the model instance bound to this CRUD.

        Args:
            fresh: When ``True``, always return a fresh, unattached model
                instance instead of caching it on the CRUD object.
        Returns:
            A model instance constructed from the kwargs provided at CRUD
            initialization.
        """
        if fresh:
            return cast(ModelTypeVar, self._model(**self._kwargs))
        if self.instance is None:
            self.instance = self._model(**self._kwargs)
        return cast(ModelTypeVar, self.instance)

    def add(
        self,
        instance: ModelTypeVar | None = None,
        **kwargs: Any,
    ) -> ModelTypeVar | None:
        """Insert a new record and optionally update fields.

        Behaviour:
        - When ``instance`` is ``None``, create an instance using the default
          kwargs provided when constructing the CRUD object;
        - Otherwise merge the given ``instance`` into the current Session and
          apply any additional field updates.

        Args:
            instance: Optional existing model instance to persist. If omitted,
                a new instance is created from the CRUD default kwargs.
            **kwargs: Field updates applied to the target instance before it
                is flushed.
        Returns:
            The persisted instance with any database-generated fields populated,
            or ``None`` when an error occurred and was handled according to
            the configured ``error_policy``.
        """
        try:
            if instance is None:
                instance = self.create_instance()

            self._ensure_nested_txn()

            need_merge = False
            insp = cast(Any, sa_inspect(instance))
            bound_sess = object_session(instance)
            need_merge = (not insp.transient) or (
                bound_sess is not None and bound_sess is not self._session
            )

            session = self._require_session()
            target = (
                cast(ModelTypeVar, session.merge(instance)) if need_merge else instance
            )

            if kwargs:
                updated = self.update(target, **kwargs)
                if updated is not None:
                    target = updated

            session.add(target)
            session.flush()
            self._need_commit = True
            self._mark_dirty()
            return target
        except SQLAlchemyError as exc:
            self._on_sql_error(exc)
        except Exception as exc:
            self.error = exc
            self.status = SQLStatus.INTERNAL_ERR
        return None

    def add_many(
        self,
        instances: list[ModelTypeVar],
        **kwargs: Any,
    ) -> list[ModelTypeVar] | None:
        """Bulk-insert multiple records, applying shared field updates.

        Args:
            instances: List of model instances to be persisted.
            **kwargs: Field updates applied to each instance before flushing.
        Returns:
            A list of managed instances after flush (potentially merged) when
            successful, an empty list when ``instances`` is empty, or ``None``
            when an error occurred and was handled according to the configured
            ``error_policy``.
        """
        try:
            if not instances:
                return []

            self._ensure_nested_txn()

            managed_instances: list[ModelTypeVar] = []
            for instance in instances:
                need_merge = False
                insp = cast(Any, sa_inspect(instance))
                bound_sess = object_session(instance)
                need_merge = (not insp.transient) or (
                    bound_sess is not None and bound_sess is not self._session
                )

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
        except Exception as exc:
            self.error = exc
            self.status = SQLStatus.INTERNAL_ERR
        return None

    def query(
        self, *args, pure: bool = False, **kwargs
    ) -> CRUDQuery[ModelTypeVar, ModelTypeVar]:
        """Construct a query with default filter conditions applied.

        Args:
            *args: Additional positional filter criteria applied via
                ``Query.filter``.
            pure: When ``True``, ignore instance-level default filters (such as
                kwargs passed to the CRUD constructor) and global filters
                registered via ``register_global_filters``.
            **kwargs: Additional keyword-style filter criteria applied via
                ``Query.filter_by``.
        Returns:
            A ``CRUDQuery`` wrapping the underlying SQLAlchemy ``Query``.
        """
        session = self._require_session()
        base_query = self._get_query_builder()(self._model, session)
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
        except Exception as exc:
            self.error = exc
            self.status = SQLStatus.INTERNAL_ERR
            self._log(exc, self.status)
        return CRUDQuery(self, final_query)

    def first(
        self, query: CRUDQuery[ModelTypeVar, ModelTypeVar] | None = None
    ) -> ModelTypeVar | None:
        """Execute a query and return the first record.

        Args:
            query: Optional existing ``CRUDQuery`` to execute. When ``None``,
                this method internally calls ``self.query()``.
        Returns:
            The first matched model instance, or ``None`` if no record matches
            or an error occurred and was handled according to ``error_policy``.
        """
        if query is None:
            query = self.query()
        return query.first()

    def all(
        self, query: CRUDQuery[ModelTypeVar, ModelTypeVar] | None = None
    ) -> list[ModelTypeVar]:
        """Execute a query and return all records.

        Args:
            query: Optional existing ``CRUDQuery`` to execute. When ``None``,
                this method internally calls ``self.query()``.
        Returns:
            A list of matched model instances (possibly empty). Errors are
            handled according to the configured ``error_policy``.
        """
        if query is None:
            query = self.query()
        return query.all()

    def update(
        self, instance: ModelTypeVar | None = None, **kwargs
    ) -> ModelTypeVar | None:
        """Update a single record's field values.

        Args:
            instance: Specific instance to update. When ``None``, the method
                first resolves an instance using ``self.query().first()``.
            **kwargs: Field updates applied to the resolved instance.
        Returns:
            The updated instance when a target record was found and the update
            succeeded; ``None`` when no target instance was found or when an
            error occurred and was handled according to ``error_policy``.
        """
        try:
            if instance is None:
                instance = self.query().first()

            if not instance:
                return None

            self._ensure_nested_txn()
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
        except Exception as exc:
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
        """Delete a single record or multiple records.

        Args:
            instance: Concrete instance to delete. When provided, ``query`` and
                ``all_records`` are ignored.
            query: Optional query used to resolve records to delete when
                ``instance`` is not provided.
            all_records: When ``True``, delete all records matched by ``query``;
                when ``False``, delete only the first matched record.
            sync: Synchronization strategy passed through to SQLAlchemy's
                ``Query.delete`` when ``all_records=True``.
        Returns:
            ``True`` if deletion was attempted and the transaction was marked
            dirty; ``False`` when no target was found or an error occurred and
            was handled according to the configured ``error_policy``.
        """
        try:
            session = self._require_session()
            if instance:
                self._ensure_nested_txn()
                session.delete(instance)
            else:
                if query is None:
                    query = self.query()

                first_inst = query.first()
                if not first_inst:
                    self.status = SQLStatus.NOT_FOUND
                    return False

                self._ensure_nested_txn()
                if all_records:
                    query.delete(synchronize_session=sync)
                else:
                    session.delete(first_inst)

            self._need_commit = True
            self._mark_dirty()
            return True
        except SQLAlchemyError as exc:
            self._on_sql_error(exc)
        except Exception as exc:
            self.error = exc
            self.status = SQLStatus.INTERNAL_ERR
        return False

    def mark_for_commit(self) -> None:
        """Mark the current context as needing commit on exit.

        This is useful when changes are made through other means (for example
        via ``crud.session``) and you still want the CRUD context manager to
        commit when leaving the ``with`` block.
        """
        self._ensure_nested_txn()
        self._need_commit = True
        self._mark_dirty()

    def commit(self) -> None:
        """Explicitly commit the current sub-transaction or Session.

        Normally, commit is handled automatically on context manager exit; this
        method is provided for advanced scenarios where you want to take
        explicit control. It also clears the internal ``_need_commit`` flag.
        """
        try:
            session = self._require_session()
            if self._nested_txn and getattr(self._nested_txn, "is_active", False):
                self._nested_txn.commit()
            else:
                session.commit()
            self._explicit_committed = True
            self._need_commit = False
        except Exception as exc:
            self._logger("CRUD commit failed: %s", exc)
            if self._session is not None:
                self._session.rollback()

    def discard(self) -> None:
        """Explicitly roll back the current transaction and discard changes.

        - Never raises an exception; callers can continue their logic.
        - Uses the internal ``_discarded`` flag so that ``__exit__`` knows to
          roll back.
        """
        try:
            session = self._require_session()
            if self._nested_txn and getattr(self._nested_txn, "is_active", False):
                self._nested_txn.rollback()
            else:
                session.rollback()
        finally:
            self._need_commit = False
            self._discarded = True

    @property
    def logger(self) -> ErrorLogger:
        """Expose the configured error logger for integration helpers."""
        return self._logger

    def _log(self, error: Exception, status: SQLStatus = SQLStatus.INTERNAL_ERR):
        """Log an error related to the current model."""
        model_name = getattr(self._model, "__name__", str(self._model))
        self._logger(
            "CRUD[%s]: <catch: %s> <except: (%s)>",
            model_name,
            error,
            status,
        )

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # Non-SQLAlchemy exceptions are always re-raised.
        # Whether SQLAlchemyError is re-raised is controlled by ``error_policy``
        # and handled by the transaction decorator or ``_on_sql_error``.
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
                if self._nested_txn and getattr(self._nested_txn, "is_active", False):
                    try:
                        self._nested_txn.rollback()
                    except Exception:
                        # Log and continue to top-level rollback handling.
                        self._logger("CRUD sub-txn rollback failed", exc_info=True)
                self._need_commit = False
            elif self._need_commit and not self._explicit_committed:
                try:
                    if self._nested_txn and getattr(
                        self._nested_txn, "is_active", False
                    ):
                        self._nested_txn.commit()
                except Exception as exc:
                    self._logger("CRUD sub-txn commit failed: %s", exc)
                    raise

            # Adjust depth via the shared transaction state; only the outermost
            # scope performs commit/rollback on the Session.
            if self._session is not None:
                session = self._session
                state = _get_txn_state(session)
                joined_existing = getattr(self, "_joined_existing", False)

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
                        except Exception as exc:
                            self._logger("CRUD commit/rollback failed: %s", exc)
                            try:
                                session.rollback()
                            except Exception:
                                pass
                            raise
        finally:
            # Session lifecycle is owned by the outer application/framework.
            self._session = None

    def _ensure_nested_txn(self) -> None:
        """Ensure there is an active SAVEPOINT / nested transaction if possible."""
        if not (self._nested_txn and self._nested_txn.is_active):
            try:
                session = self._require_session()
                self._nested_txn = session.begin_nested()
            except Exception:
                self._nested_txn = None

    def _mark_dirty(self) -> None:
        # The current transaction join/depth is managed by the shared
        # transaction state machine; this is a placeholder for future hooks.
        return

    def _on_sql_error(self, e: Exception) -> None:
        """Handle a ``SQLAlchemyError`` and optionally re-raise it."""
        self.error = e
        self.status = SQLStatus.SQL_ERR
        try:
            session = self._require_session()
            if self._nested_txn and getattr(self._nested_txn, "is_active", False):
                self._nested_txn.rollback()
            else:
                session.rollback()
        except Exception:
            self._logger("CRUD SQL rollback failed", exc_info=True)
        self._need_commit = False
        # Only re-raise SQLAlchemy errors when ``error_policy == "raise"``;
        # the transaction decorator or caller will handle the exception.
        if self.resolve_error_policy() == "raise":
            raise e

    @classmethod
    def transaction(
        cls,
        *,
        error_policy: ErrorPolicy | None = None,
        join_existing: bool = True,
        # nested: bool | None = None,
    ) -> TransactionDecorator[P, R]:
        """Function-level transaction decorator.

        - One function call == one CRUD-related transaction scope.
        - Uses the generic ``transaction(...)`` helper to implement join
          semantics and commit/rollback behaviour.
        """

        resolved_policy: ErrorPolicy = (
            error_policy if error_policy is not None else cls._default_error_policy
        )

        def session_factory() -> SessionLike:
            provider = cls._get_session_provider()
            return provider()

        return _txn_transaction(
            session_factory,
            join_existing=join_existing,
            # nested=nested,
            error_policy=resolved_policy,
        )
