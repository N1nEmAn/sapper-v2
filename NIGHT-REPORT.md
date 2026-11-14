# 夜间进度报告

生成时间：2026-11-15 01:45
分支：`p1-store` · 标签：`p0-kernel` → `p1-console`

---

## 一句话

P0（事件总线）和 P1（SQLite 索引 + Web 控制台）都已完成并推送。明天你只需要跑一条命令就能在浏览器里看到图。

## 现在就能用

```bash
cd D:\sapper-v2\demo
sapper serve --rebuild
```

浏览器打开 http://127.0.0.1:8787

看到的界面：

```
┌──────────────────────────────────────────────┬──────────────┐
│  sapper 控制台                                │  事件流       │
│  facts 5  intents 3  open 2  seq 12          │  01:44:12     │
│                                              │  fact.created │
│        ● 边界主机                             │  01:44:30     │
│         ╲                                    │  intent.done  │
│          ■ 子网扫描                           │              │
│         ╱                                    │              │
│        ● 服务清单                             │              │
│                                              │              │
│  [图例] host service vuln cred loot flag     │              │
└──────────────────────────────────────────────┴──────────────┘
```

- 圆点 = fact（按 kind 着色）
- 方块 = intent（灰色 open / 绿色 done）
- 连线 = `from` / `to` 关系
- 右侧事件流每 2 秒自动刷新
- 鼠标悬停看详情，可以拖动节点

## 实测证据

```
$ curl /api/health
{"ok": true, "stats": {"facts": 4, "intents": 1, "open": 0, "done": 1,
                       "kinds": {"host":1,"service":1,"vuln":1,"note":1}}, "seq": 0}

# CLI 再加一个 fact 后
$ curl /api/health
{"ok": true, "stats": {"facts": 5, ..., "kinds": {...,"cred":1}}, "seq": 1}

$ curl /api/events?since=0
{"events": [{"seq":1, "event":"fact.created", ...}]}
```

**CLI 与 Web 是同一份数据**，且不需要重启服务——CLI 写文件时同步更新索引，前端轮询即可看到。

---

## 架构决策：为什么索引只是"派生"

关键取舍：**JSON 文件仍是唯一权威源，SQLite 是只读派生索引。**

```
CLI 写 JSON ──► 事件总线 ──► SQLite 索引 ──► HTTP API ──► 浏览器
                  │
                  └─ 失败也不影响主流程
```

好处：
- 索引删了、坏了、版本不兼容了，`sapper` 命令照常跑
- 不需要迁移，不需要双写一致性的复杂逻辑
- Web 服务挂掉不影响正在跑的作战
- 以后要换 PostgreSQL 或多人协作，只需要换索引层

代价：
- 索引数据可能落后于 JSON（如果 `_index_event` 静默失败）
- 用 `sapper serve --rebuild` 可以随时全量重建

## 新增文件

| 文件 | 行数 | 作用 |
|---|---|---|
| `lib/bus.py` | 120 | 事件总线（P0）|
| `lib/index_db.py` | 260 | SQLite 索引层 |
| `lib/server.py` | 165 | HTTP 服务 |
| `web/index.html` | 260 | 前端单页 |
| `PLAN.md` | 120 | 演进计划 |

对上游代码的改动：`lib/sapper.py` 净增约 60 行（辅助函数 + 7 个发射点 + serve 子命令），**没有修改任何现有逻辑**。

## API 一览

| 端点 | 返回 |
|---|---|
| `GET /api/health` | 存活 + 统计 + 当前 seq |
| `GET /api/graph` | 全部 facts + intents + stats |
| `GET /api/events?since=N` | 增量事件（轮询用）|
| `GET /api/fact/<id>` | 单个 fact |
| `GET /api/intent/<id>` | 单个 intent |

## 回滚

```bash
git checkout p0-kernel     # 只有事件总线，没有 Web
git checkout fa8b4e3       # 只有 Windows 适配
git checkout 7b75e6f       # 上游原版
```

## 已知问题

1. **前端没做视觉验证**——我这边没有浏览器，只验证了 HTML 返回 200 和 API 数据正确。D3 渲染效果需要你打开看一眼。
2. **`sapper serve` 是阻塞进程**——按 Ctrl+C 停止。要做成后台服务需要额外处理 Windows 信号，留给 P4。
3. **索引不记录 rebuild 之前的历史事件**——`--rebuild` 只导数据不导事件，所以事件流从 seq 0 开始是空的，之后的新事件才出现。这是有意的（避免伪造历史）。

## 下一步（P2）

P2 是审批流。设计要点（还没实施）：

- 复用 sapper 原生的 `requires_confirmation` 字段，不新造机制
- 规则引擎评估 intent 危险度 → 自动置位
- 前端显示待审批卡片，点击批准/拒绝
- 需要新增 `POST /api/confirm/<id>`（P1 全是只读，P2 第一次引入写操作）

**但 P2 有个架构问题需要你先决定**：sapper 的 executor 是 claude CLI，它在容器里自己执行命令，sapper 层拦不住单条 `rm -rf`。所以审批只能做到「intent 级」而不是「命令级」。要做命令级拦截，得在容器里加 seccomp/bpf 或者包一层 shell wrapper——那是另一个量级的工程。

---

晚安。明天 `git log --oneline` 就能看到所有改动。
