# flask-sqlalchemy-crud

一个面向 SQLAlchemy 的轻量级 CRUD/事务辅助库（Flask glue 可通过扩展方式接入）：
- `with CRUD(Model) as crud:` 提供上下文式 CRUD 与子事务
- `@CRUD.transaction()` 支持 join 语义的函数级事务
- `error_policy="raise"|"status_only"` 可选错误策略，日志接口可自定义
- 类型友好的 `CRUDQuery` 链式查询包装

> 仓库仍在重构阶段，API 可能会有改动。

## 安装

```bash
pip install flask-sqlalchemy-crud
# 或
pip install -e .
```

需要 Python 3.11+，依赖 `sqlalchemy>=1.4`（Flask 相关可按需自行安装）。

## 快速开始（纯 SQLAlchemy）

```python
from sqlalchemy import String, Integer, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from flask_sqlalchemy_crud import CRUD

engine = create_engine("sqlite:///./crud_example.db", echo=False)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):  # type: ignore[misc]
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
    print("fetched", crud.first())
```

## 函数级事务示例

```python
from flask_sqlalchemy_crud import CRUD

CRUD.configure(session_provider=SessionLocal, error_policy="raise")

@CRUD.transaction(error_policy="raise")
def create_two_users():
    with CRUD(User) as crud1:
        crud1.add(email="a@example.com")
    with CRUD(User) as crud2:
        crud2.add(email="b@example.com")

create_two_users()
```

- 最外层调用负责提交/回滚；内层 `CRUD` 上下文遇到异常仅标记状态，最终由装饰器处理。
- `error_policy="status_only"` 会在回滚后吞掉 SQLAlchemyError，由调用方检查 `crud.status` / `crud.error`。

## 示例与文档

- 完整示例：`docs/examples/basic_crud.py`
- 事务重构设计与 TODO：`docs/crud_refactor_todo.md`
- 类型增强方向：`docs/todo.md`

## 运行测试

1. 在环境变量或 `.env` 中设置可访问的数据库 URI：`TEST_DB=sqlite:///./test.db`（或其他驱动）。
2. 安装测试依赖后执行：
   ```bash
   pytest -q
   ```

## 提示

- 主线支持纯 SQLAlchemy；Flask 相关可通过扩展方式集成。
- 使用前请先调用 `CRUD.configure(session_provider=...)` 配置会话。
