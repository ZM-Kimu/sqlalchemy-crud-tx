"""Pagination primitives and helpers for CRUDQuery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

_T = TypeVar("_T")


@dataclass(slots=True)
class PaginationResult(Generic[_T]):
    """Pagination payload returned by ``CRUDQuery.paginate``."""

    items: list[_T]
    page: int
    per_page: int
    total: int | None
    pages: int
    has_prev: bool
    has_next: bool
    prev_num: int | None
    next_num: int | None


class _PaginationQuery(Protocol[_T]):
    def count(self) -> int: ...

    def limit(self, limit: int | None) -> "_PaginationQuery[_T]": ...

    def offset(self, offset: int | None) -> "_PaginationQuery[_T]": ...

    def all(self) -> list[_T]: ...


def paginate_query(
    query: _PaginationQuery[_T],
    *,
    page: int = 1,
    per_page: int = 20,
    error_out: bool = False,
    max_per_page: int | None = None,
    count: bool = True,
) -> PaginationResult[_T]:
    """Paginate results using generic ``count/limit/offset/all`` operations."""
    if max_per_page is not None:
        per_page = min(per_page, max_per_page)

    if page < 1:
        if error_out:
            raise ValueError("page must be >= 1")
        page = 1
    if per_page < 1:
        if error_out:
            raise ValueError("per_page must be >= 1")
        per_page = 20

    offset = (page - 1) * per_page
    has_prev = page > 1
    prev_num = page - 1 if has_prev else None

    if count:
        total = query.count()
        pages = (total + per_page - 1) // per_page if total > 0 else 0
        if error_out and total > 0 and page > pages:
            raise ValueError("page is out of range")
        items = query.limit(per_page).offset(offset).all()
        has_next = page < pages
        next_num = page + 1 if has_next else None
        return PaginationResult(
            items=items,
            page=page,
            per_page=per_page,
            total=total,
            pages=pages,
            has_prev=has_prev,
            has_next=has_next,
            prev_num=prev_num,
            next_num=next_num,
        )

    batch = query.limit(per_page + 1).offset(offset).all()
    has_next = len(batch) > per_page
    items = batch[:per_page] if has_next else batch
    next_num = page + 1 if has_next else None
    return PaginationResult(
        items=items,
        page=page,
        per_page=per_page,
        total=None,
        pages=0,
        has_prev=has_prev,
        has_next=has_next,
        prev_num=prev_num,
        next_num=next_num,
    )
