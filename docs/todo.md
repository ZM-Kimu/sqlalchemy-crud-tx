# flask_sqlalchemy_crud TODO 列表

> 作用：记录后续可以迭代的“高级玩法”与设计方向，避免在核心代码中堆积过多注释。  
> 粗分为：**当前重构主线（3）** + **类型/事务增强（1/2）** + **工程化收尾（文档/测试）。**

---

## 1. 列类型级别的强类型推导（长期 / 实验性质）

- 目标：
  - 从 SQLAlchemy 模型定义中推导/复用精确的 Python 类型（优先复用 `Mapped[T]` 注解，而不是复杂 runtime 分析）。
  - 让 `CRUDQuery` 在 `with_entities` / `scalar` 等方法上具备“列级别”的类型推断能力。

- 可能的技术方案：
  - 定义 `TypedColumn[T]` 或 `InstrumentedAttr[T]` 协议，用于约束：
    - `.type.python_type` / `.impl` 或其他可用的类型信息（仅在 `TYPE_CHECKING` 中使用）。
  - `CRUDQuery` 使用两个类型参数：

    ```python
    TModel = TypeVar("TModel", bound=ORMModel)
    TRow = TypeVar("TRow", covariant=True)

    class CRUDQuery(Generic[TModel, TRow]): ...
    # 默认 TRow = TModel
    ```

    示例：
    - `CRUDQuery[User, User]`（默认全模型）
    - `with_entities(User.id)` → `CRUDQuery[User, int]`
    - `with_entities(User.id, User.name)` → `CRUDQuery[User, tuple[int, str]]`
  - 定义精简版 `QueryLike` 协议，统一描述库内部实际用到的方法：
    - `filter` / `filter_by` / `order_by` / `limit` / `offset` / `with_entities` / `scalar` / `one` 等，
    - 降低对 SQLAlchemy 官方 stubs 的耦合度。

- 兼容性与实现细节：
  - 评估不同版本 SQLAlchemy / Flask-SQLAlchemy 的 stubs 差异。
  - 初期仅在 `typing.TYPE_CHECKING` 分支中实现高级类型，运行时保持零成本。
  - 该项为长期增强，不阻塞核心解耦与发布。

---

## 2. 更细粒度的事务与 Session 类型建模（增强）

- 为 `SessionLike` 增补：
  - 对 `begin_nested` / `no_autoflush` 等上下文管理器方法的协议与测试用例。
  - 限定 `SessionLike` 只包含库内部真正使用的方法（`add` / `delete` / `commit` / `rollback` / `begin` / `begin_nested` / `execute` / `scalar` 等），避免过宽协议。
  - （可选）区分读写操作的类型标签（只读事务 vs 写事务）。

- 为 `CRUD.transaction` 装饰器定义类型安全接口：
  - 使用 `ParamSpec` + `TypeVar`，保持装饰后的函数签名与原函数一致，而不是退化为 `Any`。

    ```python
    P = ParamSpec("P")
    R = TypeVar("R")

    @overload
    def transaction(...) -> Callable[[Callable[P, R]], Callable[P, R]]: ...
    ```

  - 明确目前仅支持同步函数，是否支持 async/`async_transaction` 作为后续议题。

- （可选）研究使用 `contextvars` 存储当前事务上下文：
  - 使嵌套 `CRUD.transaction` / `with CRUD(...)` 能安全复用同一 Session / 事务。
  - 是否引入 savepoint / `Session.begin_nested` 的统一策略，待后续单独设计。

---

## 3. CRUD 解耦与重构路线（当前主线，属 BREAKING CHANGE）

### 3.1 核心接口与抽象（以 SQLAlchemy 为主线）✅（已完成）

- Session 统一（必需）：  
  - `SessionProvider = Callable[[], SessionLike]`，只能通过 provider 获取 Session，未配置 `_session_provider` 即在首次使用时抛 `RuntimeError`（强制要求 `CRUD.configure(session_provider=...)`）。  
  - 弃用类属性 `session` 等旧入口，视为 BREAKING CHANGE。
- Query 统一：  
  - `QueryFactory = Callable[[type[TModel], SessionLike], CRUDQuery[TModel, TRow]]`，查询与事务共用同一 Session。  
  - 默认从 SQLAlchemy Session 构造，不再依赖 Flask-SQLAlchemy 的 `model.query` 作为主线。
- `CRUD.configure` 规范（BREAKING）：  
  - 仅接受 `session_provider`（或显式 `session` 立刻封装为 provider）；旧的直接挂载类属性方案视为废弃并移除兼容期。  
  - `query_factory` 可选覆盖；内部归一化为类级 `_session_provider` / `_query_factory`；未配置则报错并有测试。

- 类型约束与基线：
  - 使用最小 `ORMModel` 协议作为 `TModel` 上界，移除对 `flask_sqlalchemy.model.Model` 的硬依赖（BREAKING）。
  - Python 基线锁定 3.11（不再提供 3.10 回退方案）。

### 3.2 适配层与用法（Flask 作为可选 glue，而非主线）

- 核心库仅依赖 SQLAlchemy；Flask 相关作为可选 glue，不是主线。
- Flask 集成放在独立模块（如 `flask_integration.py`）中：
  - 提供 `configure_flask(db)` 等封装，内部通过 `db.session` + 默认 `QueryFactory` 调用 `CRUD.configure`。
  - 该模块中才导入 Flask/Flask-SQLAlchemy，核心模块不直接导入。
- `pyproject.toml` 中通过 extras 管理 Flask 依赖：

  ```toml
  [project.optional-dependencies]
  flask = ["flask>=2.3", "flask-sqlalchemy>=3.1"]
  ```

- 文档提供两套 quickstart：
  - 主线：纯 SQLAlchemy（`sessionmaker` + 默认 `query_factory`）。
  - 可选：Flask 集成示例（`configure_flask(db)`）。

### 3.3 事务与行为（行为语义固定）

- 保持现有 join/error_policy 语义：
  - 嵌套装饰器/上下文的 join/savepoint 规则写清楚，并有对应测试。
  - `error_policy` 的传递/覆盖行为文档化，并有测试。
- 外部已 `begin` 的策略：
  - 暂不改动，保持现状行为，待主线重构完成后再决策（记录为后续议题）。
- 明确误用行为并固定预期：
  - 未调用 `CRUD.configure` 的报错信息。
  - 在 `CRUD.transaction` 中手动 `commit/rollback` 是否允许，若不允许则抛特定错误并测试。

### 3.4 测试矩阵

- 纯 SQLAlchemy 内存 SQLite 测试：
  - CRUD 增删改查、事务 join/rollback、冲突/误用场景。
- 保留/调整现有 Flask 集成测试：
  - 确保可选适配层不回归；CI 中默认跑 SQLAlchemy 主线，并在安装 extras 的 job 中跑 Flask。
- CI 配置：
  - 至少覆盖 Python 3.11 / 3.12。
  - 覆盖 SQLAlchemy 2.x 的一到两个主版本。
  - Flask 集成测试放在单独 job（安装 `flask` extra）中运行，保证整体稳定性。
- 误用场景测试文档化：
  - 缺 `configure`、关闭 session 后复用、外部 begin 冲突、事务内部手动 commit/rollback 等。

### 3.5 文档与迁移

- README / README_zh：
  - 明确两套用法（主线 SQLAlchemy + 可选 Flask），说明依赖拆分与 Python 基线。
  - 给出旧版 vs 新版的对比代码片段，强调从“Flask-first”迁移到“SQLAlchemy-first”的设计变化。
- 迁移说明 / CHANGELOG：
  - `configure` 签名变化；
  - SessionProvider / QueryFactory 的默认行为；
  - 依赖拆分（核心、extras）。
- 对外 API 稳定性：
  - 通过 `flask_sqlalchemy_crud.__all__` 固定公共导出（`CRUD`、`CRUDQuery`、`configure_flask` 等），内部模块结构可自由演进。

### 3.6 实施顺序（建议）

1) 落地 `_session_provider` / `_query_factory` 接口归一，保持现有行为通过测试。  
2) 完善默认 `QueryFactory`，确保 CRUD / query / transaction 共享 Session；调整类型定义。  
3) 补充纯 SQLAlchemy 测试与示例，修正代码通过测试。  
4) 添加 Flask 适配模块与文档更新（可选路径）。  
5) 更新迁移文档、依赖声明与 CI/类型检查配置。  
6) （长期）推进 1/2 中的高级 typing & 事务建模增强。  
