# sapper-v2 — Windows 部署说明

部署位置：`D:\sapper-v2`
来源：https://github.com/N1nEmAn/sapper-v2 （master，2026-11-11 快照）
部署时间：2026-11-14
最后更新：2026-11-14 22:20（模型网关接通，dispatch 实测跑通）

---

## 1. 当前状态

| 项 | 状态 |
|---|---|
| 源码 | `D:\sapper-v2`（29 MB，含 `docker/nuclei.zip` 28 MB） |
| CLI | `sapper` 已加入用户 PATH；`.env.local` 自动加载 |
| 启动器 | `bin\sapper_win.py` + `bin\sapper.cmd`（cmd/PS）+ `bin\sapper`（bash） |
| 模型网关 | ✅ DeepSeek `deepseek-flash`（= V4.1-Flash），key 在 `.env.local` |
| Agent 引擎 | ✅ Claude Code CLI 2.1.325（`bin\claude.exe`） |
| 真实调度 | ✅ `sapper dispatch reason` 实测产出 intent |
| 离线知识库 | 56 条中文条目，`sapper kb search` 可用 |
| 图可视化 | `sapper view` 生成自包含 HTML |
| Docker 全军团 | ⏳ 待导入 Ubuntu 发行版（见第 6 节） |

**上游源码零修改。** 新增内容全部在 `bin\`、`.env.local`、`demo\`。

## 2. 三个 Windows 适配点（关键）

### 2.1 fcntl — 项目文件锁

`lib\sapper.py` 第 25 行 `import fcntl`、第 185 行 `fcntl.flock()`。Windows 没有这个模块。

`bin\sapper_win.py` 在 runpy 执行 sapper 之前，往 `sys.modules` 注入一个兼容模块
（用 `msvcrt.locking()` 锁文件第 0 字节，阻塞/非阻塞语义与原版一致）。
**不改一行上游代码**，Docker 里的 Linux 版照旧用真 fcntl。

### 2.2 subprocess 找不到 `claude`

Windows 的 `CreateProcess` 只解析 `.exe`，不认 `.cmd`。所以把原生二进制
`claude.exe`（372 MB）复制到 `bin\`（已在 PATH），`subprocess.run(["claude", ...])` 才能命中。

### 2.3 模型名不是产品名

DeepSeek 的 Anthropic 兼容端点只认 **`deepseek-flash`** 和 **`deepseek-v4-pro`**。
传 `DeepSeek-V4.1-Flash` 会直接报：

```
The supported API model names are deepseek-flash, deepseek-v4-pro
```

所以 `SAPPER_MODEL=deepseek-flash`。`deepseek-chat` 也能用，但不是 V4.1-Flash。

## 3. 配置

`D:\sapper-v2\.env.local`（本机专用，勿提交）：

```ini
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_AUTH_TOKEN=sk-...
ANTHROPIC_MODEL=deepseek-flash
SAPPER_MODEL=deepseek-flash
NO_PROXY=api.deepseek.com,127.0.0.1,localhost
```

`bin\sapper.cmd` 和 `bin\sapper` 启动时都会自动读这个文件，**不需要手动 set 环境变量**。
`NO_PROXY` 是必需的：本机代理（127.0.0.1:11678）对公网不通，会拖死 API 调用。

换供应商只改这三行（BASE_URL / AUTH_TOKEN / MODEL），代码不用动。

## 4. 用法

```bash
cd D:\your-engagement
sapper setup --goal "对 example.com 做授权渗透评估" --agents

sapper dispatch reason              # 指挥官读图 → 产出新 intent（走 deepseek-flash）
sapper status                       # 看板 + frontier
sapper dispatch i003 --skip-permissions   # 执行某个 intent（真跑命令）
sapper auto --skip-permissions      # 一路自动推进到 goal

sapper fact -t "边界主机" -k host -d host=10.0.0.5 -d port=8080 "8080 返回 200"
sapper done i001 --fact "可利用" -k vuln -d cve=CVE-2020-1938
sapper pick / recall / kb search ssrf / view --out graph.html / validate
sapper teardown [--purge]
```

事实类型 `-k`：`host` `service` `cred` `vuln` `loot` `flag` `note`

## 5. 已实测

- `sapper --version` → 1.0.0；`setup`/`fact`/`intent`/`claim`/`done`/`status`/`validate` 全通
- `claude -p --output-format json --model deepseek-flash` → `{"result":"FLASH_OK"}`
- `sapper dispatch reason`（V4.1-Flash）→ 产出 i003「子网主机发现」、i004「读取敏感配置」，
  带中文标题、具体命令、预期证据、时间盒
- 再跑一次 → 模型判定 "open intents already cover it"，不重复生成（说明真在图推理）

## 6. Docker 全军团（已完成）

三个镜像已构建成功（2026-11-15 00:18）：

```
sapper-agent:commander    122ac52bd4e5    6.27 GB   1.65 GB
sapper-agent:latest       f429334ef49e    5.82 GB   1.49 GB
sapper-agent:tmux         6b0729a7d05e    5.82 GB   1.49 GB
```

内容：Kali 全套渗透工具 + claude-code 2.1.325（含 348 MB linux-x64 二进制）
+ pwntools/ROPgadget/pycryptodome/sympy + sapper 源码与 56 条知识库。

### 环境构成

| 组件 | 说明 |
|---|---|
| WSL 3.0.5 | 独立组件（`C:\Program Files\WSL\`），替换了 Win10 19045 自带的旧版 |
| Ubuntu 22.04.5 | 导入到 `D:\wsl\sapper-ubuntu`，内核 6.18.58.1-microsoft-standard-WSL2 |
| Docker 29.1.3 | WSL 内直接装 Engine（非 Docker Desktop），`nohup dockerd` 启动，不依赖 systemd |
| 构建脚本 | `D:\wsl-setup\finish-all.bat` → `finish.sh` |

### 踩过的坑（按出现顺序）

| 现象 | 根因 | 解法 |
|---|---|---|
| `wsl --shutdown` 后 LxssManager 卡死 StopPending，进程杀不掉 | 旧版 WSL 的 systemd 未生效时 `systemctl` 挂死 VM | 换 WSL 3.0.5 独立组件；脚本彻底不用 systemd |
| 代理怎么都起不来，7890 无监听 | **Windows 保留了 7860–7959 端口段**（`netsh int ipv4 show excludedportrange`）| 换 17890 |
| bat 双击闪退 | 文件是 LF 换行，Windows 批处理需 CRLF | 全部转 CRLF，`od -c` 验证 |
| `docker build` 立即失败且无日志 | legacy builder 不支持 `--progress=plain`，错误被 grep 吞掉 | 去掉该参数，全量 tee |
| pip 装 filebytes 报 `ast.Str` 不存在 | Kali 的 Python 已到 **3.14**，filebytes 还在用 3.12 就移除的 API | 源码打两处补丁（`ast.Str`→`ast.Constant`、`.s`→`.value`），COPY 进镜像本地装 |
| tmux 层从日本源拉索引 | Kali 用 `/etc/apt/sources.list.d/` deb822 格式，只改 `sources.list` 不生效 | 接受（tmux 包小），或同时改 deb822 文件 |

### 用法

```bash
# 单兵作战（最快验证）
wsl -d sapper-ubuntu -u root -- docker run --rm -it sapper-agent:latest bash

# 全军团（需要模型网关 + 平台地址）
wsl -d sapper-ubuntu -u root -- docker run --rm -it --network host \
  -e BENCHMARK_BASE_URL=https://your-platform \
  -e BENCHMARK_TOKEN=xxx \
  -e ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic \
  -e ANTHROPIC_AUTH_TOKEN=sk-... \
  -e SAPPER_MODEL=deepseek-flash \
  sapper-agent:commander
```

`BENCHMARK_BASE_URL` 是 commander 入口的硬校验项（`docker/commander/entrypoint.sh:33`），
不打比赛时需要自建 mock 平台，参考 `docker/local-sim.sh` 的思路。

### 重建镜像

```bash
chmod +x D:\wsl-setup\finish-all.bat   # 双击即可
# 或命令行：
wsl -d sapper-ubuntu -u root -- bash /mnt/d/wsl-setup/finish.sh
```

改源版 Dockerfile 在 `docker/Dockerfile.local`（上游 `docker/Dockerfile` 未改动）。

## 6.5 Git Bash 的两个坑（Windows 专有）

**坑一：POSIX 路径被改写。** 在 Git Bash 里 `sapper fact -d path=/WEB-INF/web.xml` 会把
`/WEB-INF/web.xml` 当成路径转成 `D:/.../WEB-INF/web.xml`，数据就脏了。这是 MSYS 的行为，
不是 sapper 的。传绝对路径类数据时关掉转换：

```bash
MSYS_NO_PATHCONV=1 sapper fact -t "web.xml" -k loot -d path=/WEB-INF/web.xml "确认可读"
```

`bin/sapper` 已用 `cygpath` 把入口路径转成 Windows 路径，所以关掉转换后启动器本身不受影响
（早期版本会报 `can't open file 'C:\d\...'`）。用 CMD / PowerShell 没有这个问题。

**坑二：控制台不能用 CDN。** `cdn.jsdelivr.net` 在国内多数网络不可达，页面会白屏。
d3 已本地化到 `web/vendor/d3.min.js`（279 KB，随仓库分发），控制台不再依赖任何外网。
页签支持锚点，可直接开 `http://127.0.0.1:8787/#assets` 或 `#traffic`。

## 7. 卸载

```powershell
# 1. 从用户 PATH 移除 D:\sapper-v2\bin
# 2. Remove-Item -Recurse -Force D:\sapper-v2
# 3. 项目残留：在项目目录执行 sapper teardown --purge
# 4. WSL 部分：wsl --unregister sapper-ubuntu ; 移除 D:\wsl
```

## 8. 合规

仓库自带 `DISCLAIMER.md`：仅限**授权**安全测试。军规面向竞赛靶场与授权评估，
无授权目标勿用。
