"""Generic transaction state machine and decorator implementation."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import Literal, ParamSpec, TypeAlias, TypeVar, cast

from sqlalchemy.exc import SQLAlchemyError

from .types import SessionLike, SessionProvider

P = ParamSpec("P")
R = TypeVar("R")

ErrorPolicy = Literal["raise", "status_only"]

TransactionDecorator: TypeAlias = Callable[[Callable[P, R]], Callable[P, R]]


class _TxnState:
    """Transaction state associated with a single Session.

    Shared between the generic transaction state machine and CRUD contexts to
    track:
    - join depth (``depth``);
    - whether there is an active transaction (``active``).
    """

    __slots__ = ("session", "depth", "active")

    def __init__(self, session: SessionLike) -> None:
        self.session: SessionLike = session
        self.depth: int = 0  # current join depth
        self.active: bool = False  # whether there is an active transaction


_TxnMap: TypeAlias = dict[int, _TxnState]

_current_txn_map: ContextVar[_TxnMap] = ContextVar("_current_txn_map")
_current_error_policy: ContextVar[ErrorPolicy | None] = ContextVar(
    "_current_error_policy"
)


def _get_txn_map() -> _TxnMap:
    """Return the transaction state mapping for the current ContextVar scope.

    The mapping uses ``id(Session)`` as the key and stores the corresponding
    ``_TxnState``.
    """
    try:
        return _current_txn_map.get()
    except LookupError:
        mapping: _TxnMap = {}
        _current_txn_map.set(mapping)
        return mapping


def _get_txn_state(session: SessionLike) -> _TxnState | None:
    """Return the transaction state associated with a Session, if any."""
    return _get_txn_map().get(id(session))


def _get_or_create_txn_state(session: SessionLike) -> _TxnState:
    """Get or create the transaction state for the given Session.

    The transaction state machine uses this structure to implement join/nested
    semantics.
    """
    mapping = _get_txn_map()
    key = id(session)
    state = mapping.get(key)
    if state is None:
        state = _TxnState(session)
        mapping[key] = state
    return state


def get_current_error_policy() -> ErrorPolicy | None:
    """Return the current ``error_policy`` from the ContextVar, if any."""
    try:
        return _current_error_policy.get()
    except LookupError:
        return None


class _TxnContext:
    """Basic building block for a transaction context manager.

    Currently only used as an internal helper:
    - obtains a Session via ``SessionProvider``;
    - ensures there is a ``_TxnState`` associated with that Session.

    Commit/rollback behaviour is handled by the generic ``transaction(...)``
    decorator.
    """

    __slots__ = ("_session_provider", "_session", "_state")

    def __init__(self, session_provider: SessionProvider) -> None:
        self._session_provider = session_provider
        self._session: SessionLike | None = None
        self._state: _TxnState | None = None

    @property
    def session(self) -> SessionLike:
        assert self._session is not None
        return self._session

    @property
    def state(self) -> _TxnState | None:
        return self._state

    def __enter__(self) -> "_TxnContext":
        session = self._session_provider()
        self._session = session
        self._state = _get_or_create_txn_state(session)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> Literal[False]:
        # Commit/rollback logic is handled by the generic transaction decorator;
        # this context itself never touches the database.
        return False


def transaction(
    session_provider: SessionProvider,
    *,
    join_existing: bool = True,
    # nested: bool | None = None, TODO: Implement soon
    error_policy: ErrorPolicy = "raise",
) -> TransactionDecorator[P, R]:
    """Generic transaction decorator.

    - Each function call corresponds to a "transaction scope", unless the call
      joins an already active transaction according to the ``join_existing`` rules.
    - Default join semantics: if there is an active transaction for the same
      Session, join it and let only the outermost call perform commit/rollback.
    - ``error_policy`` only affects ``SQLAlchemyError``:
        - ``"raise"``: rollback and then re-raise the database error;
        - ``"status_only"``: rollback and swallow the database error so
          callers can inspect status via other channels;
        - non-database exceptions always cause rollback and are re-raised.
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            # Obtain Session and its transaction state mapping.
            session = session_provider()
            state = _get_txn_state(session)

            # Whether to join an existing transaction.
            joining_existing = bool(
                join_existing and state is not None and state.active
            )

            token = None

            try:
                # If there is no active transaction, create state and begin one.
                if not joining_existing:
                    state = _get_or_create_txn_state(session)
                    state.depth = 0
                    state.active = True
                    try:
                        session.begin()
                    except Exception:
                        state.active = False
                        raise
                    token = _current_error_policy.set(error_policy)

                assert state is not None
                state.depth += 1

                captured_exc: BaseException | None = None
                result: R | None = None

                try:
                    result = func(*args, **kwargs)
                    return result
                except BaseException as exc:
                    captured_exc = exc

                    if not joining_existing:
                        try:
                            session.rollback()
                        except Exception:
                            # Rollback failure should not mask the original exception.
                            pass

                    is_db_error = isinstance(exc, SQLAlchemyError)

                    if not is_db_error:
                        raise

                    # DB errors may be re-raised depending on error_policy
                    if error_policy == "raise":
                        raise

                    # error_policy == "status_only": swallow SQLAlchemyError,
                    # caller (e.g., CRUD) should record status separately.
                    return cast(R, None)
                finally:
                    # Only adjust depth while state is still active.
                    if state.active:
                        state.depth -= 1
                        if state.depth <= 0:
                            state.active = False
                            # Commit only when outermost and no exception.
                            if captured_exc is None and not joining_existing:
                                try:
                                    session.commit()
                                except Exception as commit_exc:
                                    # On commit failure, attempt rollback then re-raise.
                                    try:
                                        session.rollback()
                                    except Exception:
                                        pass
                                    raise commit_exc
            finally:
                if token is not None:
                    _current_error_policy.reset(token)

        return wrapper

    return decorator
