"""类型别名与 Session 类型定义。"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

from sqlalchemy.orm import Session as _Session
from sqlalchemy.orm import scoped_session as _ScopedSession

if TYPE_CHECKING:
    from .query import CRUDQuery


@runtime_checkable
class ORMModel(Protocol):
    """最小模型能力约束，便于与纯 SQLAlchemy/Flask-SQLAlchemy 兼容。"""

    __table__: Any


ModelTypeVar = TypeVar("ModelTypeVar", bound=ORMModel)
ResultTypeVar = TypeVar("ResultTypeVar", covariant=True)
EntityTypeVar = TypeVar("EntityTypeVar")

ErrorLogger = Callable[..., None]

# 在本库中，SessionLike 视作 SQLAlchemy ORM Session 或其 scoped_session 包装。
# 这样 IDE 和类型检查器可以完整复用 SQLAlchemy 自带的类型注解，
# 同时兼容 Flask-SQLAlchemy 提供的 scoped_session[Session] 类型。
SessionLike = _Session | _ScopedSession[_Session]

SessionProvider = Callable[[], SessionLike]
QueryFactory = Callable[
    [type[ModelTypeVar], SessionLike], "CRUDQuery[ModelTypeVar, Any]"
]
