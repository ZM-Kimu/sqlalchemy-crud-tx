# flask_sqlalchemy_crud TODO 列表

> 作用：记录后续可以迭代的“高级玩法”与设计方向，避免在核心代码中堆积过多注释。

## 1. 列类型级别的强类型推导

- 目标：
  - 从 SQLAlchemy 模型列定义中推导出精确的 Python 类型。
  - 让 `CRUDQuery` 在 `with_entities` / `scalar` 等方法上具备“列级别”的类型推断能力。

- 可能的技术方案：
  - 定义 `TypedColumn[T]` 或 `InstrumentedAttr[T]` 协议，用于约束：
    - `.type.python_type` / `.impl` 或其他可用的类型信息。
  - 让模型字段在类型上表现为 `TypedColumn[int]` / `TypedColumn[str]` 等：
    - `with_entities(User.id)` 推导为 `CRUDQuery[User, int]`
    - `with_entities(User.id, User.name)` 推导为 `CRUDQuery[User, tuple[int, str]]`
  - 定义 `QueryLike` 协议，统一描述 `filter` / `filter_by` / `with_entities` / `scalar` / `one` 等签名，减少对 SQLAlchemy 官方 stubs 的直接耦合。

- 兼容性与实现细节：
  - 需要评估不同版本 SQLAlchemy / Flask-SQLAlchemy 的 stubs 差异。
  - 初期可以在 `typing.TYPE_CHECKING` 分支中实现高级类型，仅对静态检查工具可见，运行时尽量保持零成本。

## 2. 更细粒度的事务与 Session 类型建模

- 为 `SessionLike` 增补：
  - 对 `begin_nested` / `no_autoflush` 等上下文管理器方法的协议化测试用例。
  - 区分读写操作（只读事务 vs. 写事务）的类型标签（可选）。

- 考虑为 `CRUD.transaction` 装饰器定义 Protocol：
  - 让装饰后的函数返回值类型保持原样，而不是退化为 `Any`。

