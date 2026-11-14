# sapper-v2 演进计划

## 基线

| 项 | 值 |
|---|---|
| tag | `baseline`（commit 36ca68d）|
| 内容 | 上游原版 + Windows 部署适配（Dockerfile.local / 启动器 / 文档）|
| 已验证 | Docker 三镜像可用、DeepSeek 网关通、dispatch 出 intent |

回滚命令：`git checkout baseline`

## 总目标

从「CLI + JSON 文件」演进到「服务 + SQLite + Web 控制台」。

三条不可破的约束：

1. **CLI 永远可用** —— 不起服务也能完整跑 dispatch/auto
2. **stdlib only** —— 不引入 Python 第三方依赖（前端用 CDN）
3. **每阶段可独立回滚** —— 每个 tag 都是能跑的完整状态

## 阶段表

| 阶段 | 交付 | 验收标准 | 状态 | tag |
|---|---|---|---|---|
| **P0** | 内核抽象（Graph + Bus）| dispatch 输出逐字一致 | ✅ 完成 | `p0-kernel` |
| **P1** | SQLite 索引 + `sapper serve` + 图页面 | 浏览器能看到图实时增长 | ✅ 完成 | `p1-console` |
| **P2** | 守卫 + 审批 + 审计 | 越界/破坏被拦，Web 可批准 | ✅ 完成 | `p2-audit` |
| **P3** | MITM 流量录制 | 请求入库，可按域名/状态码搜索 | 未开始 | — |
| **P4** | 资产图 + 跨任务复用 + enrich | 新任务自动继承历史资产 | 未开始 | — |

## P0 详细设计

### 交付物

**1. `lib/graph.py` —— Graph API**

```python
class Graph:
    def __init__(self, root: Path)          # 指向 .sapper/
    # 写
    def add_fact(kind, title, data=(), from_ids=(), evidence=None) -> str
    def add_intent(title, from_ids, doc, box=None, creator="human") -> str
    def claim(intent_id) -> bool
    def done(intent_id, facts=()) -> dict
    # 读
    def fact(fid) -> dict
    def intent(iid) -> dict
    def frontier() -> list[dict]
    def status() -> dict
    def all_facts() -> list[dict]
    def all_intents() -> list[dict]
```

后端：现有 `.sapper/{facts,intents}/<id>.json`，**输出格式与现状逐字节一致**。

**2. `lib/bus.py` —— 事件总线**

```python
class Bus:
    def on(event: str, handler: Callable) -> None
    def emit(event: str, **payload) -> int      # 返回成功执行的 handler 数
```

语义：
- 同步执行（P0 不引入线程，避免并发问题）
- **异常隔离**：单个 handler 抛错只记录，不影响其他 handler 与主流程
- 事件名常量：`fact.created` `intent.created` `intent.claimed` `intent.done` `graph.changed`

**3. `lib/sapper.py` 改造**

采用**适配器模式**，最小侵入：

- 新增 `_graph()` 返回 `Graph` 实例
- `cmd_fact` / `cmd_intent` / `cmd_done` / `cmd_status` 内部改调 Graph
- 在 5 个关键点 `bus.emit(...)`
- **不删除任何现有函数**，保留原实现作为 `_legacy_*` 备查

### 验收清单

- [ ] `sapper status` 输出与 baseline 逐字一致（diff 为空）
- [ ] `sapper dispatch reason --dry-run` 输出逐字一致
- [ ] `sapper fact` 创建的文件内容与 baseline 一致（除时间戳）
- [ ] `sapper done` 后图结构与 baseline 一致
- [ ] `tests/test_sapper.py` 全绿
- [ ] evidence：改动前后各跑一次，日志存档

### 回滚

```bash
git checkout baseline -- lib/           # 只回滚代码
git checkout baseline                   # 全量回滚
```

## P1 详细设计（概要）

| 模块 | 内容 |
|---|---|
| `lib/store.py` | SQLite 后端，实现与 Graph 相同的接口（换后端不改上层）|
| `schema.sql` | `facts` `intents` `assets` `edges` `traffic` `approvals` 六表 |
| `lib/server.py` | `http.server` + 路由，`GET /api/{graph,assets,intents}`，SSE `/api/events` |
| `web/index.html` | 单页：D3 力导向图 + 状态面板，CDN 引入，零构建 |
| `sapper serve` | 启动服务，默认 `127.0.0.1:8787` |

数据迁移：`sapper migrate` 把现有 `.sapper/*.json` 灌进 SQLite，保留 JSON 作为备份。

## 风险与对策

| 风险 | 对策 |
|---|---|
| 4726 行改动点分散，容易改漏 | 适配器模式 + 逐函数替换 + 每次改完立刻 diff 输出 |
| 上游无测试覆盖 | 先补 3 个黄金用例（fact/intent/done），作为回归基线 |
| P0 抽象过重，反而难维护 | 只抽 8 个方法，不做 Repository 模式、不做 ORM |
| SQLite 并发写 | WAL 模式 + 单写者（sapper 本身是单进程）|
| Web 前端膨胀 | 单文件 HTML，不引入 npm，样式手写 |

## 时间线（按每晚 2 小时计）

```
P0  第 1 晚        ← 今晚
P1  第 2-3 晚
P2  第 4-5 晚
P3  第 6-8 晚
P4  第 9-11 晚
```

每期结束打 tag，跑一次完整验收，写入 CHANGELOG.md。
