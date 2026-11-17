# 托管模式配置清单（TSecBench / TPCTF-CTFy）

支持两套比赛平台后端，由 `SAPPER_PLATFORM`（`tsecbench` / `ctfy`）选择；不设时
按环境变量自动推断：同时存在时 `BENCHMARK_*` 优先，否则 `CTFY_*` 走 CTFy
（见 `lib/arena.py:_env_platform`）。

> CTFy 后端、VPN 自动接入与镜像 sudoers 是 v18 发布之后加入的：请从源码构建
> `docker/Dockerfile.commander`；v18-champion 预构建镜像不含这些改动。

## TSecBench 正式赛规则要点（2026-10-17 00:00 — 10-18 23:59）

- **只有 1 次评测机会**，仅队长账号可提交；Agent 异常没有重试
- 50 题 / 100 flag，任务限时 **8 小时**（镜像内默认按 470 分钟跑，留安全边际）
- 无 hint（接口返回 null，也无惩罚）
- 排名顺序：**得分 → 有效耗时 → token 量**（所以默认不开 verify 双检，省 token）
- 模型限流/超时/中断不算平台异常——镜像内已带**模型调用重试**（SAPPER_RETRY_MODEL=3，指数退避），单题异常自动跳过下一轮再试，绝不中断整个任务
- 严禁引导 Agent 攻击平台——提示词中已有 scope 纪律，请勿在模板里加任何"打平台"的引导

## TPCTF 2026 / CTFy 规则要点

- 平台基址 `https://ctfy.tsinghua.edu.cn/api/v1`，鉴权 `Authorization: Bearer <CTFY_TOKEN>`
- **动态 flag**：每队、每实例独立；必须用本队 `instance_id` 提交，抄别队的 flag 无效
- **按题（question）提交**：一题可含多个子问（如 `flag1`/`flag2`），各自独立计分、独立尝试次数；
  必须带 `--question <qid>`，猜答案会消耗尝试次数
- **实例并发上限由平台下发**：入围赛 1、初选赛/正式赛 3；以
  `max_instances_per_team_effective` 为准，board 心跳显示 `instances=active/cap`
- **模型网关**：初选赛/正式赛必须使用平台网关（`ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY`，
  或平台给出的等价配置）；入围赛使用队伍自己的 key。两种变量名本项目都支持
- 附件分题目级与 `per_team` 实例级；交互式题目均为 per-instance WireGuard
  VPN（`network_topology=engagement`），配置里可能带 `DNS=` 与 `*.internal` 入口
- VPN 连接由 `lib/arena.py` 统一处理：去掉 `DNS=` 行（避免 wg-quick 改写
  `/etc/resolv.conf`，模型网关仍需公网 DNS），用配置里的 DNS 解析 `*.internal`
  后写入 `/etc/hosts` 的 sapper 标记块；`--down` 时删除标记块并关隧道
- 比赛暂停/未开赛时 start/submit 返回 `competition_paused` / `competition_not_in_session`
  ——这是等待信号，不是题目失败，不要反复重试
- 同样**严禁引导 Agent 攻击平台**：scope 仅限本队 brief 里列出的题目容器，不碰
  平台 API、计分、VPN 网关、出题系统或其他队伍实例

## 一、平台凭据

### A. TSecBench（控制台自动注入，不要手填）

| 变量 | 说明 |
|---|---|
| `BENCHMARK_BASE_URL` | 答题 API 基址，任务创建后自动注入 |
| `BENCHMARK_TOKEN` | 答题 API 凭证，自动注入 |

### B. CTFy / TPCTF（运行时手填或由平台注入）

| 变量 | 说明 |
|---|---|
| `CTFY_BASE_URL` | 默认 `https://ctfy.tsinghua.edu.cn/api/v1`；只填域名也可，客户端会补 `/api/v1` |
| `CTFY_TOKEN` | 平台 Bearer Token（`pf_...` / `pt_...`），**不要写进日志/报告** |
| `CTFY_COMPETITION_ID` | 可选。仅一个已报名进行中比赛时自动选择；多比赛并存时必须指定 |
| `CTFY_VERIFY_TLS` | 可选，`0` 表示跳过 TLS 校验（仅在平台证书链不完整时使用） |
| `SAPPER_PLATFORM` | 可选，强制后端：`ctfy` 或 `tsecbench` |
| `SAPPER_VPN_AUTO` | 可选，默认 `1`：spawn 时下载并自动拉起 per-instance WireGuard；`0` 只下载配置（容器需 `--cap-add=NET_ADMIN`） |

## 二、运行时环境变量（控制台手填）

| KEY | VALUE | 说明 |
|---|---|---|
| `ANTHROPIC_BASE_URL` | `https://open.bigmodel.cn/api/anthropic` | 直连厂商公开端点；TPCTF 初选/正式赛填平台网关地址 |
| `ANTHROPIC_AUTH_TOKEN` | 你的智谱 API Key | 与下一行二选一；换供应商只改这项和 BASE_URL |
| `ANTHROPIC_API_KEY` | 你的 API Key | 平台网关要求的变量名；entrypoint/watchdog/worker 均已同步支持 |
| `SAPPER_MODEL` | `glm-5.3-flash[1m]` | 已实测走通。`[1m]` 后缀由 claude CLI 解析为 1M 上下文（剥后缀 + beta 头），API 侧模型名仍是 glm-5.3-flash，直接填 `glm-5.3-flash-1m` 会报模型不存在 |

### Key 池自动轮换（强烈建议配置）

当某个 Key 额度耗尽/401/欠费，系统自动切换到池中下一组凭据，`-p` 版**无需重启**（下一次模型调用即用新 Key），tmux 版自动轮换并重启对应 TUI（`claude --continue` 恢复原会话上下文）。检测特征：insufficient/balance/quota/exceeded/billing/401/unauthorized/欠费/余额。

| KEY | VALUE 格式 |
|---|---|
| `SAPPER_KEY_POOL` | `[{"base_url":"https://open.bigmodel.cn/api/anthropic","token":"智谱key1","model":"glm-5.3-flash[1m]"},{"base_url":"https://api.moonshot.cn/v1","token":"moonshot-key","model":"kimi-k2.5"}]` |

注意：池里各组 URL 直连各厂商公开端点。单 Key 时轮换自动退化为普通重试。

可选调参（不填用镜像默认值）：

| KEY | 默认 | 说明 |
|---|---|---|
| `SAPPER_TOTAL_MINUTES` | `470` | 总预算（8h 限时留边际） |
| `SAPPER_MINUTES_PER_CHALLENGE` | `9` | 每题每轮 auto 分钟数；bench 多轮循环，没解完的题下轮续（进度在图里保留）。50 题×9 分≈450 分 |
| `SAPPER_MAX_STEPS` | `10` | 每题每轮最多 reason 轮数 |
| `SAPPER_RETRY_MODEL` | `3` | 模型调用失败重试次数（10s/20s/40s 退避） |
| `SAPPER_VERIFY` | 不设 | 设为 `1` 才开启核验者（费 token，正式赛默认不开） |

## 三、容器启动流程

1. 校验平台凭据与模型网关环境变量，缺失立刻 FATAL（日志可见）：
   TSecBench 查 `BENCHMARK_*`；CTFy 查 `CTFY_TOKEN`，网关查
   `ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_API_KEY`
2. 平台 API 连通性探测（TSecBench: `GET /openapi/v1/challenges` 带
   `BENCHMARK_TOKEN`；CTFy: `GET /api/v1/competitions` 带 Bearer）+ 模型 CLI 冒烟测试
3. commander 架构（默认）：
   `entrypoint.sh` 起 tmux 会话 → commander TUI 读 `SKILL.md` → `worker.sh` 按
   board 决策 spawn/kill/msg → 每题 spawn 时 `arena start`，自动复用本队已运行实例、
   受平台并发上限约束，并把实例地址/凭据/附件/VPN/子问状态写入
   `<code>/.sapper/brief.md` → worker 用
   `sapper flag "<answer>" -c <code> --question <qid> --submit` 提交 →
   完成或放弃时 `kill <code> --close` 提交余量并关闭实例
   非 commander 的 `sapper bench` 路径同样走双后端，逐题
   `start → 建图（题目描述/靶标地址按不可信数据录入）→ auto 自主解题
   → 提交已记录未提交的 flag（正确/错误都回写）→ close`
4. 非特权用户运行（Claude Code 拒绝 root 下跳权限确认）；密钥全走环境变量，镜像不含任何密钥/题目记忆
5. VPN：per-challenge 配置下载到 `<code>/vpn/sapper-<hash8>.conf`。commander 的
   `worker.sh spawn` 默认 `SAPPER_VPN_AUTO=1`，会在实例 ready 后立即
   `sapper arena vpn <code> --up`：去掉 `DNS=`、起 WireGuard、把 `*.internal`
   解析进 `/etc/hosts`（`network_topology=simple` 的直连实例跳过）。容器必须 `--cap-add=NET_ADMIN`；镜像已用 sudoers 只放行
   `wg-quick` 与 `tee /etc/hosts`。设 `SAPPER_VPN_AUTO=0` 可退回只下载；失败只记日志，
   不阻塞 spawn。若配置没有 `DNS=` 行，`*.internal` 无法映射，brief 会给出 warning，
   此时改用平台返回的服务 IP/端口。手工排查：`sapper arena vpn <code> --down`
   关隧道并还原 hosts

## 四、一次机会的检查单（提交前自查）

- [ ] 用**其他题集**（如 Tsecbench v1 公开集 / CTFy 测试题）跑过完整托管评测，`correct_flag_count` 有增长
- [ ] 镜像内 `sapper --version`、`sapper bench --help`、`sapper arena --help` 正常
- [ ] `ANTHROPIC_*` / `ANTHROPIC_API_KEY` 填的是对应阶段允许的网关地址 + 正确 key
  （TPCTF 初选/正式赛必须平台网关，入围赛用队伍自己的 key）
- [ ] CTFy：`CTFY_TOKEN` 有效、`sapper arena competitions` 能看到目标比赛、
  `sapper arena challenges` 能列出题目且 `unique_code` 与文件名一致
- [ ] CTFy：确认当前比赛并发上限（board `instances=0/<cap>`），commander 不会按 3 硬开
- [ ] CTFy：多子问题目确认 brief 里 `--question <qid>` 正确；本地不会自动猜子问
- [ ] 队长账号提交；活动期间只此一次
- [ ] 报告/日志/工单中不回显 `BENCHMARK_TOKEN`、`CTFY_TOKEN`、API Key

## 五、CTFy 常用命令（人工排查）

```bash
sapper arena init --platform ctfy          # 写 .sapper/arena.json 模板（token 可写 ENV:CTFY_TOKEN）
sapper arena info                          # 非敏感配置 + 并发用量（cap/active/available）
sapper arena competitions                  # 比赛列表：id/phase/报名状态
sapper arena challenges                    # 当前比赛的题目：unique_code/子问/完成度
sapper arena challenge <challenge_id>      # 题目详情（子问提示、附件、是否关闭）
sapper arena start <challenge_id>          # 开实例（自动复用已有实例；受并发上限约束）
sapper arena instance <instance_id>        # 实例：entry_urls/services/子问状态/VPN
sapper arena instances                     # 本队全部实例
sapper arena brief <challenge_id>          # 输出靶标简报（实例/凭据/子问/提交命令）
sapper arena attachments <challenge_id> --dir ./attachments
sapper arena vpn <challenge_id> --out ./vpn/vpn.conf    # 下载配置
sapper arena vpn <challenge_id> --up                    # 去 DNS 行、起隧道、映射 *.internal
sapper arena vpn <challenge_id> --down                  # 关隧道并删除 /etc/hosts 标记块
sapper flag "<answer>" -c <challenge_id> --question <qid> --submit
sapper flag '["A","B"]' -c <challenge_id> -q <qid> --answer-json --submit
sapper arena stop <challenge_id>           # 关实例（幂等；deadline 时 watchdog 也会兜底）
```

注意：`sapper arena info` 只显示 `base_url`，不显示 token；任何日志都不要打印
`CTFY_TOKEN` / `BENCHMARK_TOKEN`。
