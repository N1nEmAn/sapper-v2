"""Persistent plan: the memory a stateless commander lacks.

sapper's commander is invoked fresh on every `dispatch reason`. That is good
for token economy and terrible for multi-step exploitation: a chain like
"找到注入点 → 拿凭据 → 横向 → 提权" has real ordering, and a stateless planner
either front-loads all four in one round (chaos) or forgets step 3 exists.

ARTEX solves this with a TodoStore that outlives planner wake-ups and is
re-injected into each wake-up prompt. This is the same idea, kept as plain JSON
next to the graph so it is diffable, greppable, and hand-editable.

Design constraints:

* **One step in progress at a time.** A plan with four parallel "doing" items
  is not a plan, it is a wish list. `set --status doing` demotes the previous.
* **A step may be linked to an intent.** That is how the plan learns whether
  work actually happened, instead of asking the model to remember.
* **Blocked is a first-class state.** Most real chains stall on a missing
  credential; recording *why* is what makes the next wake-up smarter.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

PLAN_NAME = "plan.json"
STATUSES = ("todo", "doing", "done", "blocked", "dropped")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_path(root: Path | str) -> Path:
    return Path(root) / PLAN_NAME


class Plan:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.path = plan_path(self.root)

    # -- storage -----------------------------------------------------------
    def load(self) -> dict:
        if not self.path.exists():
            return {"steps": [], "updated_at": None, "note": ""}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"steps": [], "updated_at": None, "note": ""}
        data.setdefault("steps", [])
        return data

    def save(self, data: dict) -> dict:
        data["updated_at"] = _now()
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        tmp.replace(self.path)
        return data

    def exists(self) -> bool:
        return self.path.exists()

    # -- editing -----------------------------------------------------------
    def add(self, title: str, *, after: int | None = None, note: str = "") -> dict:
        title = (title or "").strip()
        if not title:
            raise ValueError("步骤标题不能为空")
        data = self.load()
        steps = data["steps"]
        step = {
            "n": (max((s["n"] for s in steps), default=0) + 1),
            "title": title,
            "status": "todo",
            "depends_on": [after] if after else [],
            "intent": None,
            "note": note,
            "created_at": _now(),
            "updated_at": _now(),
        }
        steps.append(step)
        self.save(data)
        return step

    def find(self, n: int) -> dict:
        for step in self.load()["steps"]:
            if step["n"] == n:
                return step
        raise ValueError(f"计划里没有第 {n} 步")

    def set_status(self, n: int, status: str, *, note: str = "") -> dict:
        if status not in STATUSES:
            raise ValueError(f"status 必须是 {'/'.join(STATUSES)}")
        data = self.load()
        target = None
        for step in data["steps"]:
            if step["n"] == n:
                target = step
                break
        if target is None:
            raise ValueError(f"计划里没有第 {n} 步")
        if status == "doing":
            # one in progress; demote the previous without touching its note
            for step in data["steps"]:
                if step["status"] == "doing" and step["n"] != n:
                    step["status"] = "todo"
                    step["updated_at"] = _now()
        target["status"] = status
        target["updated_at"] = _now()
        if note:
            target["note"] = note
        self.save(data)
        return target

    def link(self, n: int, intent_id: str) -> dict:
        data = self.load()
        for step in data["steps"]:
            if step["n"] == n:
                step["intent"] = intent_id or None
                step["updated_at"] = _now()
                self.save(data)
                return step
        raise ValueError(f"计划里没有第 {n} 步")

    def remove(self, n: int) -> None:
        data = self.load()
        before = len(data["steps"])
        data["steps"] = [s for s in data["steps"] if s["n"] != n]
        if len(data["steps"]) == before:
            raise ValueError(f"计划里没有第 {n} 步")
        self.save(data)

    def sync_intents(self, intents: dict[str, dict]) -> list[int]:
        """Advance steps whose linked intent concluded.

        This is the only automatic transition: if the intent the step was
        delegated to is `done`, the step is done. Everything else stays manual,
        because "did we actually achieve it" is a judgement, not a lookup.
        """
        data = self.load()
        moved = []
        for step in data["steps"]:
            iid = step.get("intent")
            if not iid or step["status"] in ("done", "dropped"):
                continue
            intent = intents.get(iid)
            if intent and intent.get("status") == "done":
                step["status"] = "done"
                step["updated_at"] = _now()
                moved.append(step["n"])
        if moved:
            self.save(data)
        return moved

    # -- rendering ---------------------------------------------------------
    def ready(self) -> list[dict]:
        """Steps whose dependencies are satisfied and that are not started."""
        data = self.load()
        done = {s["n"] for s in data["steps"] if s["status"] == "done"}
        return [s for s in data["steps"]
                if s["status"] == "todo" and all(d in done for d in s.get("depends_on") or [])]

    def render(self, *, for_prompt: bool = True, limit: int = 20) -> str:
        data = self.load()
        steps = data["steps"]
        if not steps:
            return ""
        mark = {"todo": "☐", "doing": "▶", "done": "✔", "blocked": "✕", "dropped": "—"}
        lines = []
        for step in steps[:limit]:
            dep = ""
            if step.get("depends_on"):
                dep = f"  (前置 {','.join(str(d) for d in step['depends_on'])})"
            intent = f"  [{step['intent']}]" if step.get("intent") else ""
            note = f"  — {step['note']}" if step.get("note") else ""
            lines.append(f"  {mark[step['status']]} {step['n']}. {step['title']}"
                         f"{intent}{dep}{note}")
        if len(steps) > limit:
            lines.append(f"  … 另有 {len(steps) - limit} 步未显示")
        header = "【你的规划待办（跨唤醒保留，上一轮你写的）】" if for_prompt else "规划待办"
        body = "\n".join(lines)
        ready = self.ready()
        tail = ""
        if ready:
            tail = ("\n下一步可派（前置已满足）: "
                    + ", ".join(f"{s['n']}.{s['title']}" for s in ready[:3]))
        return f"{header}\n{body}{tail}"

    def progress(self) -> dict:
        data = self.load()
        steps = data["steps"]
        counts = {s: 0 for s in STATUSES}
        for step in steps:
            counts[step["status"]] = counts.get(step["status"], 0) + 1
        total = len(steps)
        done = counts.get("done", 0)
        return {"total": total, "done": done,
                "percent": round(100.0 * done / total, 1) if total else 0.0,
                **counts}
