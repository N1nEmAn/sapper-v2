# Commander Architecture SPEC — v1 (2026-10-16)

> Contract for the three implementation units (worker.sh / watchdog.sh / SKILL.md).
> Reference implementation for self-heal primitives: `docker/tmux-drive.sh` (read it first).
> All units live in `docker/commander/`, shipped to the image at `/opt/sapper/docker/commander/`.

## Runtime layout

```
tmux session "sapper-cmd"
  window 0 "commander"   — claude TUI, the AI commander loop
  window N "<pane-name>" — worker panes (TUI or -p), spawned/killed by commander
  window "watchdog"      — the dumb guardian script (stays in window 0? no: runs detached via nohup)
ROOT=/work/runs (SAPPER_ROOT)
  <code>/               — challenge home; .sapper/ fact graph lives here
  <code>/.sapper/brief.md — authoritative target brief (instance, entry URLs,
                          creds, VPN, every question id/prompt/state)
  <code>/attachments/   — challenge/instance attachments (CTFy)
  <code>/vpn/           — per-challenge VPN client config (CTFy)
  <code>-w2/            — second agent on same challenge: .sapper -> ../<code>/.sapper SYMLINK
  commander-state.md    — commander's self-maintained campaign state (handover doc)
  arena-instances.json  — CTFy live-instance cache (atomic, chmod 600)
  drive.log             — watchdog log
```

## Environment

Two challenge-platform backends, selected by `SAPPER_PLATFORM` (`ctfy|tsecbench`)
or inferred from env (BENCHMARK_* wins when both are present; see lib/arena.py):
- TSecBench: `BENCHMARK_BASE_URL/TOKEN`.
- CTFy/TPCTF: `CTFY_BASE_URL` (default `https://ctfy.tsinghua.edu.cn/api/v1`),
  `CTFY_TOKEN` (Bearer), optional `CTFY_COMPETITION_ID` (auto-selected when the
  team has exactly one registered running competition), `CTFY_VERIFY_TLS=0` to
  skip verification.

Model env: `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY`,
`SAPPER_KEY_POOL` (JSON array), `SAPPER_TOTAL_MINUTES` (default 470),
`SAPPER_MAX_WORKERS` (default 8 — hard pane cap).

**Model tiering (commander decides per worker):**
- `SAPPER_MODEL` (default `glm-5.3[1m]`) — the SMART tier. Used for: the commander's own
  TUI, and workers the commander assigns to hard chains.
- `SAPPER_MODEL_FLASH` (default `glm-5.3-flash[1m]`) — the FAST/FREE tier (unlimited
  quota). Default model for spawned workers: recon, easy challenges, -p chores.
- `worker.sh spawn --model <id>` overrides per spawn; default = `$SAPPER_MODEL_FLASH`.
- worker.sh persists each pane's model to `ROOT/pane-env/<pane>.env` (KEY=VAL, sourceable)
  so watchdog relaunches keep the same tier. Watchdog fallback: `$SAPPER_MODEL_FLASH`.
- Key-pool rotation may change provider (e.g. DeepSeek fallback): rotate_key writes
  SAPPER_MODEL per pool entry; pane relaunches after rotation use the rotated model if
  the original tier is no longer served (pragmatic: keep pane's tier unless provider
  changed base_url family — simplest: relaunch uses rotated.env SAPPER_MODEL).

Platform constraint: the per-team instance cap is platform-reported
(`max_instances_per_team_effective`: CTFy qualifiers 1, prelim/final 3;
TSecBench 3). `worker.sh board` shows `instances=active/cap` from
`ArenaClient.describe()`. Multiple agent panes on the SAME challenge share ONE
instance. CTFy instances are question-level: a challenge may have several
questions (`flag1`, `flag2`, ...), each with its own attempt cap and `requires`
unlocks; a running instance is required for submission.

## Unit 1: worker.sh — the commander's hands (called BY the commander)

`#!/usr/bin/env bash`, `set -o pipefail`. Subcommands (exit non-zero + stderr message on failure).
Must be idempotent and safe to call repeatedly. No main loop — one-shot commands.

### `spawn <code> [--tui|--p] [--mission "..."] [--minutes N] [--name NAME] [--model <id>]`
1. If no live pane named `<code>-w*` exists for this code AND platform instance not started:
   `arena start <code>` (reuse arena() inline-python helper; WATCHDOG/worker share the
   same helper contract).
   - TSecBench: start has the legacy one-flag semantics; HTTP 409 "max active" →
     friendly `PLATFORM CAP: close a challenge first`, exit 3.
   - CTFy: reuse an already-live instance for the challenge (`GET /instances`),
     local capacity pre-check against `max_instances_per_team_effective`, POST
     `/instances`, poll `starting`→`ready`, persist to `ROOT/arena-instances.json`.
     `team_capacity_exceeded`/409 also → exit 3. `container_addr` is synthesized
     from `entry_urls` + service `host:port`; VPN-only targets may have none — that
     is NOT a failure (see 1b).
   - Exit 3 message mentions the live `instances=x/y` gauge.
1b. CTFy artifacts (best effort, non-fatal): write `ROOT/<code>/.instance.json`,
   `ROOT/<code>/.sapper/brief.md` (instance id, entry URLs, credentials, VPN
   status, question ids/prompts/unlock/attempt state, exact submit commands),
   download attachments to `ROOT/<code>/attachments/`, VPN config to
   `ROOT/<code>/vpn/sapper-<hash8>.conf`.  With `SAPPER_VPN_AUTO=1` (default)
   the per-challenge WireGuard tunnel is brought up immediately: `DNS =` is
   stripped so wg-quick cannot replace the container resolver (the model
   gateway still needs public DNS), and `*.internal` targets are resolved
   through the tunnel's DNS into a marked `/etc/hosts` block.  Needs
   `--cap-add=NET_ADMIN`; the image grants passwordless sudo only for
   `wg-quick` and `tee /etc/hosts`.  Failures are logged and never block the
   pane.  Direct-access instances (`network_topology=simple`) skip the tunnel
   entirely.  `SAPPER_VPN_AUTO=0` restores download-only behaviour.
2. Prepare dirs: if primary agent (`-w1`): `mkdir -p ROOT/<code>`, `sapper init --goal "solve <code>..."`.
   If secondary (`-w2+`): `mkdir -p ROOT/<code>-wN`, `ln -sfn ../<code>/.sapper ROOT/<code>-wN/.sapper`.
3. Trust preset (copy the PYTRUST block + settings.json from tmux-drive.sh launch()).
4. TUI mode: `tmux new-window -d -n <pane> "cd ROOT/<code>[-wN] && claude --model $MODEL --dangerously-skip-permissions 2>&1; echo DRIVE_PANE_EXITED; exec sleep infinity"`
   (NO --continue: fresh session per pane; recovery uses cwd-scoped --continue by watchdog —
   different cwd ⇒ different session dir ⇒ no cross-talk between w1/w2).
5. `-p` mode: window command = `cd ... && sapper auto --store ROOT/<code>/.sapper --minutes <N|10> ... ; echo DRIVE_PANE_EXITED; exec sleep infinity`
6. Wait for input box (≤75s, auto-accepting dialogs), then `send_literal` the mission.
   Default mission (TUI): same operator prompt as tmux-drive.sh launch(), with addresses.
   --mission overrides. Retry mission once if not confirmed in pane.
7. Print `SPAWNED <pane> addrs=<...>` on success.

### `kill <pane> | <code> [--close]`
Kill window(s). `--close` also `arena close <code>` and submit recorded flags first
(reuse finish_flags + finish_challenge pattern; NOTE: only close the platform instance when
NO other pane of that code remains alive).

### `msg <pane> "<text>"` — send-keys -l + Enter (bracketed paste is unreliable — NEVER use load-buffer/paste-buffer).

### `peek <pane> [lines=40]` — capture-pane tail (commander watches worker screens).

### `list` — one line per pane: `<pane> alive|exited idle=<s> size=<cols>x<rows> since=<hh:mm>`.

### `board` — the war board. Aggregate:
- platform: `arena list` → per code: completed yes/no, flag counts
- capacity: `arena info` → `instances=active/cap` in the header, heartbeat, and --json
- graph: for each ROOT/*/. sapper/project.json → count facts by kind, open intents, flag facts
  with status recorded/submitted, latest fact mtime
- panes: worker list merged in
- output: compact aligned text table (this gets pasted into the commander TUI — keep it <100 lines;
  include a `--json` flag too).

### `hint <code> "<text>"` — append a hint fact to that challenge's graph (`sapper hint` CLI or
direct json write; hint content prefixed `[commander]`).

### Hard cap: refuse spawn when live worker panes >= SAPPER_MAX_WORKERS (exit 4, `WORKER CAP`).

## Unit 2: watchdog.sh — the guardian (dumb loop, NO decisions)

`set -o pipefail`. Single loop, sleep 15s. State in declare -A maps. Logs `[watch] ...` to drive.log.

Responsibilities IN ORDER per sweep:
1. **Commander pane dead** (DRIVE_PANE_EXITED or window gone) → relaunch claude in window
   "commander" with `--continue` (commander has its OWN cwd ROOT/commander-home so --continue
   is safe), wait for input box, inject bootstrap prompt:
   `Read /opt/sapper/docker/commander/SKILL.md and resume command. First read ROOT/commander-state.md if it exists (your handover from a previous incarnation).`
   (first boot: same but "start commanding").
2. **Unsubmitted input in commander pane** (input box `❯` + text present, no activity) → re-Enter
   (3 tries, then restart commander window).
3. **Confirm dialogs in ANY pane** → Down+Enter (accept_dialogs).
4. **Quota/auth errors in ANY pane** (QUOTA_RE from tmux-drive.sh) → rotate_key() (exact copy);
   on success restart that pane: TUI workers relaunch with cwd-scoped `--continue`;
   commander relaunch same as (1); -p workers: just log (commander will notice via board).
   Rotation never burns restart budget.
5. **TUI worker DRIVE_PANE_EXITED** → auto-relaunch with `--continue` (same cwd), max 3/pane
   (restarts map), budget-independent. -p workers exited = normal completion: leave the pane
   (commander reads result via peek, kills via worker.sh kill).
6. **Global deadline** (SAPPER_TOTAL_MINUTES from script start) → C-c all worker panes,
   finish_flags for EVERY code with a graph (question/instance-aware; deferrable CTFy
   errors leave the fact `recorded` instead of falsely `rejected`), close every live
   CTFy instance (`arena active`, exact instance id) plus every non-completed TSecBench
   challenge, log summary, exit 0. This is the failsafe finish when the commander is
   dead at the buzzer.
7. Worker TUI stall (no pane change > SAPPER_STALL_SECONDS=600) → do NOTHING (nudging is the
   commander's job; watchdog only keeps processes alive).

ALSO: watchdog must NOT do challenge selection, spawning, killing, mission text — zero strategy.
Startup: `mkdir -p ROOT`, log env summary, ensure tmux session exists (`tmux new-session -d -s sapper-cmd`), adopt-existing state.

## Unit 3: SKILL.md — the commander's brain

Frontmatter-free markdown, injected path /opt/sapper/docker/commander/SKILL.md. Sections:

1. **Role**: field commander of an autonomous pentest engagement on a CTF
   challenge platform (TSecBench or CTFy/TPCTF, 8h). You command workers; you NEVER
   run recon/exploit commands yourself. Your tools: `worker.sh` (hands) + `sapper`
   CLI (graph/arena/flag/kb) + reading panes + `ROOT/<code>/.sapper/brief.md`.
2. **The loop** (run forever until deadline): `worker.sh board` → think (who's stuck? who's
   close? what intel transfers?) → act (spawn/msg/hint/kill) → `sleep 60-120` → repeat.
   Iteration cadence: ~1-2 min. Always re-read board fresh; never assume.
3. **Tool manual**: exact worker.sh subcommand syntax + expected outputs + exit codes
   (3=platform cap, 4=worker cap) + `sapper arena challenges`, `sapper kb search`.
4. **Doctrine**:
   - Score locks at first solve: sweep EASY first (e-series look easier than f2-). Get the
     cheap board coverage before burning hours on hard chains.
   - Platform cap: platform-reported per-team instance cap (board `instances=x/y`;
     CTFy qualifiers 1, prelim/final 3). Panes are free (up to
     SAPPER_MAX_WORKERS=8); instances are not. Closing a challenge frees its instance.
   - Same-challenge multi-agent: spawn w2 with a DIFFERENT angle ("w2: try source-code
     audit path while w1 continues black-box"). They share the fact graph — w2 sees w1's
     facts. Tell workers this.
   - Worker forms: TUI for deep attacks (session persists), `-p --minutes N` for bounded
     chores (recon sweep, verify a guess). -p results appear in the pane; peek then kill.
   - Brief first: every worker reads `ROOT/<code>/.sapper/brief.md` before touching
     the target. It carries instance id, entry URLs, credentials, VPN transport, and
     question ids — the mission text must repeat the exact submit command for the
     question(s) being attacked.
   - Intel transfer: when a worker confirms a technique ("filter bypasses on ×\\u003cscript×"),
     `worker.sh hint <other-code> "[commander] e-series WAF bypass works: ..."` + msg the
     working pane. Same product+version across hosts ⇒ same CVE.
   - Stuck ladder (per challenge): tactical msg → +w2 different angle → declare parked
     (kill panes, close instance, move on). Don't let 3 hard challenges eat the clock.
   - Flags: workers submit question-level via
     `sapper flag "<answer>" -c <code> --question <qid> --submit` themselves (arrays
     add `--answer-json`). Your job: verify on board that recorded flags got submitted;
     re-run finish on close. NEVER guess: wrong answers burn attempt budget.
   - NEVER attack the platform API/scoring/VPN infrastructure, challenge authoring,
     or another team's instances. Scope = your team's briefed challenge containers.
5. **Self-management (CRITICAL)**: your context is finite. Every ~10 loop iterations (or when
   you feel context pressure / auto-compact warnings), rewrite `ROOT/commander-state.md`:
   current instance allocation, per-challenge status table (code | status | panes | next move |
   notes), parked list, intel log, deadline. Keep it <150 lines. After writing, run /compact
   (or your harness's compaction) — the state file IS your memory across compaction/restart.
   On any fresh start, FIRST action: read commander-state.md.
6. **Failure awareness**: if a pane dies, watchdog restarts it with --continue (session
   resumes) — you'll see a fresh boot; re-send context if the worker looks amnesiac. If YOU
   see quota errors, watchdog rotates keys; expect pane reboots. If worker.sh exits 3 — free
   an instance. Exit 4 — too many panes, kill one.

Tone: imperative, checklist-heavy, no fluff. The commander model is glm-5.3-flash — keep
instructions literal and unambiguous, avoid judgment calls requiring nuance.

## entrypoint.sh (unit 4, trivial)
Env check (BENCHMARK_* or CTFY_* depending on `SAPPER_PLATFORM`; platform token
mandatory; ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY) → platform reachability
(`/openapi/v1/challenges` with BENCHMARK_TOKEN, or `/api/v1/competitions` with
Bearer) + model smoke → tmux new-session -d -s sapper-cmd -n commander →
`nohup watchdog.sh &` → spawn commander claude TUI in window 0 (cwd
ROOT/commander-home) with bootstrap prompt. Container CMD stays foreground on the
watchdog (tmux attach alternative: `tail -f drive.log`).

## Dockerfile.commander
FROM sapper-agent:tmux (inherits everything), COPY docker/commander/* to /opt/sapper/docker/commander/,
chmod +x worker.sh watchdog.sh entrypoint.sh, ENTRYPOINT entrypoint.sh.

## Known traps (inherited — DO NOT regress)
- bracketed paste breaks ink TUIs → send-keys -l only
- first-boot trust dialogs → preset hasTrustDialogAccepted + Down+Enter auto-accept
- same-cwd --continue cross-talk → per-agent cwd with .sapper symlink
- arena HTTP 409 must never crash a loop → every arena call in `|| true` + message check
- bash assoc arrays for string-keyed maps → declare -A
- worker panes must end with `echo DRIVE_PANE_EXITED; exec sleep infinity` so death is detectable
- CTFy state file is shared by worker.sh/watchdog/CLI: export SAPPER_ROOT before any
  `ArenaClient.load(None)` or the instance cache lives in the wrong home
- CTFy flags are per-question and per-team-per-instance: never auto-submit when more
  than one question is open, never guess, never reuse another instance's answer
- `per_team` attachments return 409 on the challenge route → retry the instance route
- VPN interface names cap at 15 chars: configs use `sapper-<hash8>.conf`; the
  cleaned wg config lives under `vpn/.up/` with the same short stem. Concurrent
  instances get distinct interfaces/routes (the platform hands each a separate
  `AllowedIPs` /24)
- Never feed wg-quick a config with `DNS =`: it rewrites `/etc/resolv.conf` and
  breaks the public model gateway. Strip it, then resolve `*.internal` through
  the tunnel DNS into `/etc/hosts`; `vpn-down` removes the marked block
- A failed tunnel is transport failure, not challenge failure: worker logs it,
  the brief says the tunnel is not up, and public/direct targets still work
