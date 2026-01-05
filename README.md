# sqlalchemy-crud-tx

English | [中文](README_zh.md)

A lightweight CRUD + transaction helper for SQLAlchemy (Flask glue can be added via extensions):
- Context-managed CRUD with nested savepoints: `with CRUD(Model) as crud:`
- Function-level transactions via `@CRUD.transaction()` with join semantics
- Configurable error policy (`error_policy="raise"|"status_only"`) and pluggable logger
- Type-friendly `CRUDQuery` wrapper for common chainable operations

## Install

```bash
pip install sqlalchemy-crud-tx
# with Flask integration
pip install "sqlalchemy-crud-tx[flask]"
# or local editable install
pip install -e .
```

Requires Python 3.11+ with `sqlalchemy>=1.4` (optional Flask integration can be added separately).

## Quick Start (pure SQLAlchemy)

```python
from sqlalchemy import String, Integer, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy_crud_tx import CRUD

engine = create_engine("sqlite:///./crud_example.db", echo=False)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "example_user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)

CRUD.configure(session_provider=SessionLocal, error_policy="raise")

with CRUD(User) as crud:
    user = crud.add(email="demo@example.com")
    print("created", user)

with CRUD(User, email="demo@example.com") as crud:
    row = crud.first()
    print("fetched", row)

with CRUD(User) as crud:
    crud.delete(row)
# or
with CRUD(User, email="demo@example.com") as crud:
    crud.delete()
```

## Function-Level Transactions

```python
from sqlalchemy_crud_tx import CRUD

CRUD.configure(session_provider=SessionLocal, error_policy="raise")

@CRUD.transaction(error_policy="raise")
def create_two_users():
    with CRUD(User) as crud1:
        crud1.add(email="a@example.com")
    with CRUD(User) as crud2:
        crud2.add(email="b@example.com")

create_two_users()
```

- The outermost call commits or rolls back; inner CRUD contexts only mark status when exceptions occur.
- With `error_policy="status_only"`, SQLAlchemyError is rolled back and caught; check `crud.status` / `crud.error` instead.

## Docs & Examples

- Full example: `docs/examples/basic_crud.py`
- Transaction refactor notes/TODO: `docs/crud_refactor_todo.md`
- Typing directions: `docs/todo.md`

## Testing

1. Provide a DB URI via env or `.env`: `TEST_DB=sqlite:///./test.db` (or another driver).
2. Install test deps, then:
   ```bash
   pytest -q
   ```

## Notes

- SQLAlchemy-first; optional Flask integration can be layered via extensions.
- Always call `CRUD.configure(session_provider=...)` before using CRUD instances.
