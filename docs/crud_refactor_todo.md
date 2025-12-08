# CRUD / 事务重构 TODO 列表

> 本文聚焦：CRUD 模块化 + 更优雅的函数级事务方案。  
> 类型系统的更高级玩法仍参考 `docs/todo.md` 现有内容。

## 3. 模块化与依赖拆分（P1）

- [ ] 考虑适配层（未做，视需求决定是否新增）：
  - [ ] 如有需要，单独建立 `flask_integration.py` 处理与 `has_request_context` / `g` 的交互。
  - [ ] 对外仍保持简单的 `CRUD.configure(session=..., error_logger=...)` 接口。

## 5. 渐进迁移与兼容性（P1）

- [ ] 为新事务 API 编写最小示例：
  - [x] Service 函数 + `@CRUD.transaction` 示例（见 README 与 docs/examples）。
  - [ ] 无 Flask 上下文场景的使用示例（尚未提供）。

## 6. 类型与文档同步（P2）

- [ ] 与 `docs/todo.md` 中类型相关 TODO 对齐：
  - [ ] 在新的事务与装饰器设计中预留 `TypedColumn[T]`、`CRUDQuery[Model, T]` 等扩展空间。

## 7. 实施阶段分解（施工 TODO，按顺序执行）

- [ ] 第六阶段：文档与示例完善（P2）
  - [ ] 在 `docs/` 中新增简短的“事务行为示例”，涵盖典型嵌套场景（`a()` 调 `b()` / `c()` 等 join 行为）。
  - [ ] 更新 README 与项目示例代码，使用新的 `@CRUD.transaction` + `with CRUD(...)` 组合展示推荐实践。
