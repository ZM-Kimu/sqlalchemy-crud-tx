"""Enum describing SQLAlchemy operation status codes."""

from enum import IntEnum


class SQLStatus(IntEnum):
    """Status of SQLAlchemy operations."""

    OK = 0
    SQL_ERR = 1
    INTERNAL_ERR = 2
    NOT_FOUND = 5
