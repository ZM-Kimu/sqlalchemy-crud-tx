"""Lightweight wrapper around SQLAlchemy ``Query`` with type-friendly API."""

from collections.abc import Iterator
from functools import wraps
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast, overload

from sqlalchemy.orm import Query

from .types import ORMModel

if TYPE_CHECKING:
    from .crud import CRUD

ModelTypeVar = TypeVar("ModelTypeVar", bound=ORMModel)
ResultTypeVar_co = TypeVar("ResultTypeVar_co", covariant=True)

_E = TypeVar("_E")
_SENTINEL = object()


class CRUDQuery(Generic[ModelTypeVar, ResultTypeVar_co]):
    """Query wrapper used by CRUD.

    - Preserves the underlying SQLAlchemy ``Query`` functionality.
    - Adds type parameters and a chainable interface.
    - Delegates unknown attributes/methods to the wrapped ``Query``.
    - Terminal methods (``first/all/...``) call the underlying ``Query``.
    """

    __slots__ = ("_crud", "_query")

    def __init__(self, crud: "CRUD[ModelTypeVar]", query: Query) -> None:
        self._crud = crud
        self._query = query

    @property
    def query(self) -> Query:
        """Return the underlying SQLAlchemy ``Query`` object."""
        return self._query

    def _clone_with(self, query: Query) -> "CRUDQuery[ModelTypeVar, ResultTypeVar_co]":
        """Return a new CRUDQuery wrapping the given Query."""
        return cast(
            "CRUDQuery[ModelTypeVar, ResultTypeVar_co]", CRUDQuery(self._crud, query)
        )

    def join(self, *args, **kwargs) -> "CRUDQuery[ModelTypeVar, ResultTypeVar_co]":
        """Apply ``Query.join`` and return a wrapped CRUDQuery."""
        return self._clone_with(self._query.join(*args, **kwargs))

    def outerjoin(self, *args, **kwargs) -> "CRUDQuery[ModelTypeVar, ResultTypeVar_co]":
        """Apply ``Query.outerjoin`` and return a wrapped CRUDQuery."""
        return self._clone_with(self._query.outerjoin(*args, **kwargs))

    def filter(self, *criterion) -> "CRUDQuery[ModelTypeVar, ResultTypeVar_co]":
        """Apply ``Query.filter`` with the given criteria."""
        return self._clone_with(self._query.filter(*criterion))

    def filter_by(self, **kwargs) -> "CRUDQuery[ModelTypeVar, ResultTypeVar_co]":
        """Apply ``Query.filter_by`` with the given keyword filters."""
        return self._clone_with(self._query.filter_by(**kwargs))

    def distinct(self, *criterion) -> "CRUDQuery[ModelTypeVar, ResultTypeVar_co]":
        """Apply ``Query.distinct`` and return a wrapped CRUDQuery."""
        return self._clone_with(self._query.distinct(*criterion))

    def options(self, *options) -> "CRUDQuery[ModelTypeVar, ResultTypeVar_co]":
        """Apply loader options via ``Query.options`` and return a wrapped CRUDQuery."""
        return self._clone_with(self._query.options(*options))

    @overload
    def with_entities(self, entity: _E, /) -> "CRUDQuery[ModelTypeVar, _E]": ...

    @overload
    def with_entities(
        self, entity: Any, entity2: Any, *entities: Any
    ) -> "CRUDQuery[ModelTypeVar, tuple[Any, ...]]": ...

    def with_entities(
        self, entity: Any, entity2: Any = _SENTINEL, *entities: Any
    ) -> "CRUDQuery[ModelTypeVar, Any]":
        """Change the selected entities (requires at least one entity).

        - Single entity: ``CRUDQuery[Model, E]``.
        - Multiple entities: ``CRUDQuery[Model, tuple[Any, ...]]``.
        """
        if entity2 is _SENTINEL:
            new_query = self._query.with_entities(entity)
        else:
            new_query = self._query.with_entities(entity, entity2, *entities)
        return CRUDQuery(self._crud, new_query)

    def order_by(self, *clauses) -> "CRUDQuery[ModelTypeVar, ResultTypeVar_co]":
        """Apply ordering via ``Query.order_by`` and return a wrapped CRUDQuery."""
        return self._clone_with(self._query.order_by(*clauses))

    def group_by(self, *clauses) -> "CRUDQuery[ModelTypeVar, ResultTypeVar_co]":
        """Apply grouping via ``Query.group_by`` and return a wrapped CRUDQuery."""
        return self._clone_with(self._query.group_by(*clauses))

    def having(self, *criterion) -> "CRUDQuery[ModelTypeVar, ResultTypeVar_co]":
        """Apply ``Query.having`` and return a wrapped CRUDQuery."""
        return self._clone_with(self._query.having(*criterion))

    def limit(self, limit: int | None) -> "CRUDQuery[ModelTypeVar, ResultTypeVar_co]":
        """Apply a row limit via ``Query.limit`` and return a wrapped CRUDQuery."""
        return self._clone_with(self._query.limit(limit))

    def offset(self, offset: int | None) -> "CRUDQuery[ModelTypeVar, ResultTypeVar_co]":
        """Apply an offset via ``Query.offset`` and return a wrapped CRUDQuery."""
        return self._clone_with(self._query.offset(offset))

    def select_from(self, *entities) -> "CRUDQuery[ModelTypeVar, ResultTypeVar_co]":
        """Apply ``Query.select_from`` with the given entities."""
        return self._clone_with(self._query.select_from(*entities))

    def execution_options(
        self, *args, **kwargs
    ) -> "CRUDQuery[ModelTypeVar, ResultTypeVar_co]":
        """Apply execution options via ``Query.execution_options``."""
        return self._clone_with(self._query.execution_options(*args, **kwargs))

    def enable_eagerloads(
        self, value: bool
    ) -> "CRUDQuery[ModelTypeVar, ResultTypeVar_co]":
        """Enable or disable eager loading on the underlying query."""
        return self._clone_with(self._query.enable_eagerloads(value))

    def all(self) -> list[ResultTypeVar_co]:
        """Return all results from the underlying query."""
        return self._query.all()

    def first(self) -> ResultTypeVar_co | None:
        """Return the first result (or ``None``) from the underlying query."""
        return self._query.first()

    def one(self) -> ResultTypeVar_co:
        """Return exactly one result from the underlying query or raise."""
        return self._query.one()

    def one_or_none(self) -> ResultTypeVar_co | None:
        """Return one result or ``None`` from the underlying query."""
        return self._query.one_or_none()

    def scalar(self) -> ResultTypeVar_co | None:
        """Return a scalar value from the underlying query (or ``None``)."""
        result = self._query.scalar()
        return cast(ResultTypeVar_co | None, result)

    def count(self) -> int:
        """Return the row count for the underlying query."""
        return self._query.count()

    def paginate(self, *args, **kwargs):
        """Delegate to ``Query.paginate`` (Flask-SQLAlchemy style pagination)."""
        return self._query.paginate(*args, **kwargs)

    def raw(self) -> Query:
        """Return the raw underlying SQLAlchemy ``Query`` instance."""
        return self._query

    def __iter__(self) -> Iterator[ResultTypeVar_co]:
        """Iterate over results from the underlying query."""
        return iter(self._query)

    def __getitem__(self, item):
        """Delegate ``[]`` access to the underlying query."""
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
