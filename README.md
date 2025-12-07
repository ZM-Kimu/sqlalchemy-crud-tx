# flask-sqlalchemy-crud

一个针对 Flask-SQLAlchemy 的轻量级 CRUD/事务辅助库，提供：
- 上下文管理器式的 CRUD 读写与子事务（`with CRUD(Model) as crud:`）
- 函数级事务装饰器（`@CRUD.transaction()`）支持 join 语义
- 可配置的错误策略（`error_policy="raise"|"status"`）与日志接口
- 类型友好的 `CRUDQuery` 链式包装

> 仓库仍在重构阶段，API 可能会有改动。

## 安装

```bash
pip install -e .
```

需要 Python 3.10+ 且已安装 `flask-sqlalchemy>=3.0`、`sqlalchemy>=1.4`。

## 快速开始

```python
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import Mapped, mapped_column
from flask_sqlalchemy_crud import CRUD

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///./crud_example.db"
db = SQLAlchemy(app)

class User(db.Model):  # type: ignore[misc]
    __tablename__ = "example_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(db.String(255), unique=True, nullable=False)

with app.app_context():
    db.drop_all()
    db.create_all()
    CRUD.configure(session=db.session)

    with CRUD(User) as crud:
        user = crud.add(email="demo@example.com")

    with CRUD(User, email="demo@example.com") as crud:
        print(crud.first())
```

## 函数级事务示例

```python
from flask_sqlalchemy_crud import CRUD

@CRUD.transaction(error_policy="raise")
def create_two_users():
    with CRUD(User) as crud1:
        crud1.add(email="a@example.com")
    with CRUD(User) as crud2:
        crud2.add(email="b@example.com")

create_two_users()
```

- 最外层事务负责提交/回滚；内层 `CRUD` 上下文在遇到异常时仅标记状态，最终由装饰器处理。
- `error_policy="status"` 会在回滚后吞掉 SQLAlchemyError，由调用方检查 `crud.status` / `crud.error`。

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
