# CRUD / 事务重构 TODO 列表

> 本文聚焦：CRUD 模块化 + 更优雅的函数级事务方案。  
> 类型系统的更高级玩法仍参考 `docs/todo.md` 现有内容。

## 0. 设计确认（P0）

- [x] 明确函数级事务的主推使用方式：
  - [x] 提供 `@CRUD.transaction` 装饰器（service 函数层使用），一次函数调用 = 一个事务域。
  - [x] 保留 `with CRUD(Model) as crud:` 作为上下文级事务块，块内 CRUD 写操作由该块独立控制提交/回滚。
  - [x] 两种模式允许并存与嵌套使用，例如：

    ```python
    @CRUD.transaction()
    def aa():
        write_op1()  # 属于函数级事务域
        with CRUD(...):  # 与函数级事务语义上独立的上下文块
            write_op2()  # 属于上下文级事务域
            write_op3()  # 属于上下文级事务域
    ```

  - [x] 错误处理策略：默认抛出异常，但用户可以通过配置切换为“异常不抛出，仅通过 `status` / `error` 表达”。

- [x] 设计全局与局部配置机制：
  - [x] 提供类级全局配置入口 `CRUD.set_config(...)`，用于设置默认行为（如 `error_policy`、`enable_request_scoped_txn` 等）。
  - [x] 保留/扩展实例级配置入口 `CRUD(...).config(...)`，用于针对单个 CRUD 实例覆盖全局配置（如 `error_policy`、`disable_global_filter` 等）。
  - [x] 明确优先级：装饰器参数（函数级） > 实例级配置 > 类级全局配置 > 库级默认值。
  - [x] 统一错误处理策略为 `error_policy`：仅支持 `"raise"`（回滚后抛异常）与 `"status"`（回滚/标记后不抛异常，依赖 `status` + `error`）两种模式，避免再引入第三种通道。
  - [x] 避免为单次方法调用增加额外布尔开关（如 `crud.add(..., raise_on_error=False)`），将策略类配置集中在全局（set_config）/实例（config）/装饰器（transaction 参数）三层。

- [x] 确认对 Flask 请求上下文的依赖边界：
  - [x] CRUD 核心不再维护请求级根事务共享语义。
  - [x] CLI / Web 场景下的行为保持一致，如需请求级事务由上层集成自行实现。

## 0.1 明确对 Flask 请求上下文的依赖边界（P0）

> 目标：最终 **在 CRUD 核心中移除请求级事务语义**，仅保留函数级与上下文级事务；Flask 仅作为可选集成层存在。

- [x] 放弃在 CRUD 核心中维护“请求级根事务共享”的设计：
  - [x] 不再依赖 `flask.g` / `_CTX_KEY` 存储 root_txn、depth 等信息。
  - [x] 不再在核心中隐式创建“整请求大事务”，事务边界仅由显式的 `@CRUD.transaction` / `with CRUD(...)` 决定。
- [x] 统一 CLI / Web 场景下的行为：
  - [x] 无论是否运行在 Flask request context 中，`@CRUD.transaction` 与 `with CRUD(...)` 的语义一致：一次调用 / 一次上下文块 = 一个明确的事务域（或加入已有事务）。
  - [x] 如项目需要请求级事务，可在独立的集成层（例如 `flask_integration` 模块）中实现，不作为 CRUD 核心库职责。


## 1. 现有实现梳理与拆分点识别（P0）

- [x] 梳理 `crud.py` 中与事务相关的逻辑：
  - [x] `_TransactionScope` 内部流程（成功 / 失败 / nested 事务）。
  - [x] `_ensure_root_txn_cls` / `_get_request_ctx_cls` / `_CTX_KEY` 与 `flask.g` 的交互。
  - [x] `_ensure_sub_txn`、`_sub_txn`、`_need_commit`、`_explicit_committed`、`_discarded`。
  - [x] `__enter__` / `__exit__` 中对提交/回滚与日志的处理。
- [x] 标注可抽离为“与 Flask 无关的事务通用逻辑”的部分：
  - [x] 上下文管理的 `SessionLike` 使用方式与子事务处理（`begin_nested` / `commit` / `rollback`）。
  - [x] 事务标志位（`_need_commit`、`_explicit_committed`、`_discarded`）及其状态机。
- [x] 标注强依赖 Flask 的部分（需要移除或下放到集成层）：
  - [x] 基于 `flask.g` / `_CTX_KEY` / `depth` / `error` 的“请求级根事务共享”逻辑。
  - [x] 在 `__enter__` / `__exit__`、`_TransactionScope` 中对上述请求级事务状态的读写。
- [x] 重写事务管理核心（保持语义明确、实现简洁）——设计层面：
  - [x] 在不依赖 Flask 的前提下，确定新的事务上下文与状态机语义，仅表达“函数级 / 上下文级事务 + error_policy”。
  - [x] 约定上下文事务（`with CRUD(...)`）的目标行为：与 `@CRUD.transaction` 共用同一事务状态机与 join 规则，保持语义一致、结构可维护。

## 2. 函数级事务 API 设计（P0）

- [x] 设计通用事务装饰器（不依赖 Flask）——设计层面：
  - [x] 引入公共类型：
    - [x] `ErrorPolicy = Literal["raise", "status"]`
    - [x] `P = ParamSpec("P")`、`R = TypeVar("R")`
  - [x] 函数签名草案：
    - [x] `def transaction(`  
          &nbsp;&nbsp;&nbsp;&nbsp;`session_factory: Callable[[], SessionLike],`  
          &nbsp;&nbsp;&nbsp;&nbsp;`*, join: bool = True, nested: bool | None = None,`  
          &nbsp;&nbsp;&nbsp;&nbsp;`error_policy: ErrorPolicy = "raise",`  
          &nbsp;&nbsp;&nbsp;&nbsp;`) -> Callable[[Callable[P, R]], Callable[P, R]]: ...`
  - [x] 语义约定：
    - [x] 每次装饰的函数调用，对应一次“函数级事务域”，除非根据 `join` 规则加入已有事务。
    - [x] `join=True`：若当前线程/上下文中已经存在针对同一 `SessionLike` 的外层事务，则**加入该事务**，由外层决定最终 `commit/rollback`；否则新建事务。
    - [x] `nested` 预留用于高级用法（如强制使用 savepoint / “requires_new” 模式），当前阶段可以仅作为内部实现细节或暂不公开。
    - [x] `error_policy="raise"`：在适当回滚后重新抛出异常；`"status"`：在回滚并记录状态后不抛异常，由调用方检查状态。
    - [x] 默认仅对继承自 `SQLAlchemyError` 的数据库错误做“事务回滚 + 按策略处理”，非数据库异常同样触发回滚，但不做特殊包装。
  - [x] 技术实现要点（设计上已明确，待在实施阶段编码）：
    - [x] 使用线程本地或等价机制，为每个 `SessionLike` 维护当前事务深度 / 上下文栈，用于实现 `join` 规则与“最外层提交/回滚”。
    - [x] 保持装饰前后函数签名与返回值类型不变（`Callable[P, R] -> Callable[P, R]`）。

- [x] 设计基于 CRUD 的便捷装饰器——设计层面：
  - [x] 类方法签名草案：
    - [x] `@classmethod`  
          `def transaction(`  
          &nbsp;&nbsp;&nbsp;&nbsp;`cls, *,`  
          &nbsp;&nbsp;&nbsp;&nbsp;`error_policy: ErrorPolicy | None = None,`  
          &nbsp;&nbsp;&nbsp;&nbsp;`join: bool = True,`  
          &nbsp;&nbsp;&nbsp;&nbsp;`nested: bool | None = None,`  
          &nbsp;&nbsp;&nbsp;&nbsp;`) -> Callable[[Callable[P, R]], Callable[P, R]]: ...`
  - [x] 语义约定：
    - [x] 一次函数调用 = 一个 CRUD 相关的事务域；内部通过通用 `transaction(...)` 装饰器实现。
    - [x] `error_policy` 解析顺序：装饰器参数 > `CRUD` 实例级 `config()` > `CRUD.set_config()` 全局配置 > 库级默认 (`"raise"`)。
    - [x] 多层嵌套遵循“join”原则：若当前已有由同一 `SessionLike` 承载的外层事务，内层 `@CRUD.transaction` 加入该事务，不单独提交/回滚；仅最外层负责最终 `commit/rollback`。
    - [x] 若底层 `SessionLike` 已设置为事务外部共享（例如由应用框架管理生命周期），`CRUD.transaction` 仅负责事务边界与错误策略，不负责 `close/remove`。

- [x] 预留类型建模接口——设计层面：
  - [x] 定义 `TransactionDecorator` Protocol（或 TypeAlias），抽象出：  
    - [x] `TransactionDecorator[P, R] = Callable[[Callable[P, R]], Callable[P, R]]`
  - [x] 在类型提示中为 `CRUD.transaction` 返回值标注 `TransactionDecorator[P, R]`，以便 mypy / pyright 正确推断被装饰函数的参数与返回值。

## 3. 模块化与依赖拆分（P1）

- [ ] 新增 `transaction.py`（命名可再评估）：
  - [ ] 放置 `_TransactionScope` 的通用版本（不直接引用 Flask）。
  - [ ] 放置底层 `transaction(...)` 装饰器与通用事务上下文管理器。
- [ ] 精简 `crud.py`：
  - [ ] `CRUD` 内部不再直操作 Flask / `g`，只依赖 `transaction.py` 提供的接口。
  - [ ] 保留现有 CRUD 行为：`add` / `update` / `delete` / `first` / `all` 等。
- [ ] 考虑适配层：
  - [ ] 如有需要，单独建立 `flask_integration.py` 处理与 `has_request_context` / `g` 的交互。
  - [ ] 对外仍保持简单的 `CRUD.configure(session=..., error_logger=...)` 接口。

## 4. 错误处理与日志策略统一（P1）

- [ ] 明确函数级事务与 `CRUD` 方法中对错误的统一策略：
  - [ ] 是否统一在最外层事务装饰器中做日志记录。
  - [ ] 是否弱化 `CRUD.error` / `CRUD.status` 的使用，鼓励显式异常处理。
- [ ] 审视 `_log()` 与 `_error_logger` 使用场景：
  - [ ] 避免重复日志或“吞掉异常但日志信息不足”的情况。
  - [ ] 约定日志格式和字段（model 名、事务 depth、request-scoped 标记等）。

## 5. 渐进迁移与兼容性（P1）

- [ ] 保持 `with CRUD(Model) as crud:` 语义不变：
  - [ ] 内部改用新的事务工具实现，但对外行为不变。
- [ ] 新增而不立即移除旧接口：
  - [ ] 文档中标明哪些是“内部实现细节”，不建议外部依赖。
  - [ ] 如有必要，对某些成员加上“软弃用”说明（文档层面）。
- [ ] 为新事务 API 编写最小示例：
  - [ ] Service 函数 + `@CRUD.transaction` 示例。
  - [ ] 无 Flask 上下文场景的使用示例。

## 6. 类型与文档同步（P2）

- [ ] 与 `docs/todo.md` 中类型相关 TODO 对齐：
  - [ ] 在新的事务与装饰器设计中预留 `TypedColumn[T]`、`CRUDQuery[Model, T]` 等扩展空间。
- [ ] 更新 README / 使用文档：
  - [ ] 加入函数级事务与模块化后 CRUD 的推荐使用方式。
  - [ ] 标注“重构中 / API 可能变动”的区域，降低误用风险。

## 7. 实施阶段分解（施工 TODO，按顺序执行）

- [ ] 第一阶段：引入事务基础设施（P0）
  - [ ] 新增 `transaction.py` 模块，引入 `ErrorPolicy` / `P` / `R` / `TransactionDecorator` 等公共类型定义。
  - [ ] 在该模块内搭建最小可用的事务上下文框架（事务栈结构、`SessionLike` 获取约定），暂不对现有 `CRUD` 做调用。

- [ ] 第二阶段：实现通用事务状态机与装饰器（P0）
  - [ ] 在 `transaction.py` 中实现通用 `transaction(session_factory, *, join, nested, error_policy)` 装饰器，完成 join 语义与最外层 `commit/rollback` 行为。
  - [ ] 为通用装饰器补充最小级别的文档与示例（独立于 `CRUD`，仅依赖 `SessionLike`）。

- [ ] 第三阶段：重写 `CRUD.transaction` 与上下文事务（P0）
  - [ ] 在 `crud.py` 中重写 `CRUD.transaction`，使其仅作为通用 `transaction(...)` 的薄包装（解析配置 + 构造 `session_factory`）。
  - [ ] 重构 `CRUD.__enter__` / `CRUD.__exit__`，使其基于同一事务状态机表达上下文级事务，并与 `@CRUD.transaction` 共用 join 行为。

- [ ] 第四阶段：移除旧的请求级事务与 Flask 耦合代码（P1）
  - [ ] 删除或迁移 `_get_request_ctx` / `_get_request_ctx_cls` / `_ensure_root_txn*` / `_CTX_KEY` 等请求级事务相关实现。
  - [ ] 移除 `_TransactionScope` 中对 `flask.g` / `has_request_context` 的直接依赖，只保留与通用事务状态机一致的部分。
  - [ ] 若需要，为 Flask 提供可选的集成模块（如 `flask_integration.py`），但不在核心中默认启用。

- [ ] 第五阶段：统一错误处理与日志策略（P1）
  - [ ] 在新的事务实现中统一 `error_policy` 的解析路径（装饰器参数 > 实例配置 > 全局配置 > 默认值）。
  - [ ] 对 `CRUD.error` / `CRUD.status` / `_log` / `_error_logger` 的使用做一次整理，保证在 `"raise"` 与 `"status"` 模式下行为一致且文档化。

- [ ] 第六阶段：文档与示例完善（P2）
  - [ ] 在 `docs/` 中新增简短的“事务行为示例”，涵盖典型嵌套场景（`a()` 调 `b()` / `c()` 等 join 行为）。
  - [ ] 更新 README 与项目示例代码，使用新的 `@CRUD.transaction` + `with CRUD(...)` 组合展示推荐实践。
