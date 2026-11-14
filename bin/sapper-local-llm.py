#!/usr/bin/env python3
"""sapper-local-llm — a claude-CLI-compatible engine backed by local Ollama.

sapper shells out to its engine as:

    <SAPPER_BIN> -p --output-format json --model <M> \
                 [--dangerously-skip-permissions] [--append-system-prompt <SYS>]

with the prompt on stdin, and expects a JSON envelope on stdout:

    {"result": "<text>", "is_error": false}

This adapter satisfies that contract against a LOCAL model (no cloud API), and
— critically — implements the tool loop itself, because a bare local model
cannot execute anything. Tools exposed to the model:

    bash        run a shell command in the engagement cwd
    read_file   read a text file
    write_file  write a text file

Point sapper at it with:

    set SAPPER_BIN=python D:\\sapper-v2\\bin\\sapper-local-llm.py
    set SAPPER_MODEL=qwen3:8b

Env knobs:
    SAPPER_LOCAL_URL        default http://127.0.0.1:11434
    SAPPER_LOCAL_MODEL      fallback model name if --model is absent
    SAPPER_LOCAL_MAX_TURNS  tool-loop cap, default 14
    SAPPER_LOCAL_NUM_CTX    context window, default 24576
    SAPPER_LOCAL_SHELL      shell binary (auto-detected: git bash, else cmd)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

OLLAMA_URL = os.environ.get("SAPPER_LOCAL_URL", "http://127.0.0.1:11434").rstrip("/")
MAX_TURNS = int(os.environ.get("SAPPER_LOCAL_MAX_TURNS", "14"))
NUM_CTX = int(os.environ.get("SAPPER_LOCAL_NUM_CTX", "24576"))
TOOL_OUTPUT_LIMIT = 6000
CMD_TIMEOUT = 300


def find_shell() -> list[str] | None:
    """Prefer a real bash so the model can use POSIX syntax."""
    if os.environ.get("SAPPER_LOCAL_SHELL"):
        return [os.environ["SAPPER_LOCAL_SHELL"], "-c"]
    for cand in ("D:/git/Git/bin/bash.exe", "D:/git/Git/usr/bin/bash.exe"):
        if os.path.isfile(cand):
            return [cand, "-c"]
    exe = shutil.which("bash")
    if exe:
        return [exe, "-c"]
    return None


SHELL = find_shell()


def run_bash(command: str, timeout: int = CMD_TIMEOUT) -> str:
    timeout = max(5, min(int(timeout or CMD_TIMEOUT), 900))
    try:
        if SHELL:
            proc = subprocess.run(
                SHELL + [command],
                capture_output=True, timeout=timeout,
                cwd=os.getcwd(), errors="replace",
            )
        else:
            proc = subprocess.run(
                command, shell=True, capture_output=True, timeout=timeout,
                cwd=os.getcwd(), errors="replace",
            )
    except subprocess.TimeoutExpired:
        return f"[timeout] command exceeded {timeout}s: {command}"
    except Exception as exc:  # noqa: BLE001
        return f"[error] {type(exc).__name__}: {exc}"

    out = (proc.stdout or "") + (proc.stderr or "")
    if len(out) > TOOL_OUTPUT_LIMIT:
        out = out[:TOOL_OUTPUT_LIMIT] + f"\n...[truncated, {len(out)} chars total]"
    return out.strip() or f"[no output, exit={proc.returncode}]"


def read_file(path: str, limit: int = 20000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(limit)
        return data or "[empty file]"
    except Exception as exc:  # noqa: BLE001
        return f"[error] {type(exc).__name__}: {exc}"


def write_file(path: str, content: str) -> str:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return f"[written] {path} ({len(content)} chars)"
    except Exception as exc:  # noqa: BLE001
        return f"[error] {type(exc).__name__}: {exc}"


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a shell command in the engagement working directory and return "
                "combined stdout+stderr. Use it for recon, exploitation, and for the "
                "`sapper` CLI (auto, status, fact, intent, done, flag, kb)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "shell command to execute"},
                    "timeout": {"type": "integer", "description": "seconds, default 300"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from disk.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write UTF-8 text to a file (creates parent dirs).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
]

DISPATCH = {"bash": run_bash, "read_file": read_file, "write_file": write_file}


def chat(model: str, messages: list[dict], tools: list | None) -> dict:
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.25, "num_ctx": NUM_CTX},
    }
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"sapper-local-llm: cannot reach Ollama at {OLLAMA_URL} ({exc}). "
            "Start it with `ollama serve` and check `ollama list`."
        )


def parse_argv(argv: list[str]) -> tuple[str, str | None]:
    model = os.environ.get("SAPPER_LOCAL_MODEL", "qwen3:8b")
    system = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--model" and i + 1 < len(argv):
            model = argv[i + 1]
            i += 2
        elif tok in ("--append-system-prompt", "--system-prompt") and i + 1 < len(argv):
            system = argv[i + 1]
            i += 2
        elif tok.startswith("--model="):
            model = tok.split("=", 1)[1]
            i += 1
        elif tok.startswith("--append-system-prompt="):
            system = tok.split("=", 1)[1]
            i += 1
        else:
            i += 1
    return model, system


def main() -> int:
    argv = sys.argv[1:]
    model, system = parse_argv(argv)
    prompt = sys.stdin.read()
    if not prompt.strip():
        json.dump({"result": "", "is_error": True, "error": "empty prompt"}, sys.stdout)
        return 1

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    started = time.time()
    text = ""
    turns = 0
    for turns in range(1, MAX_TURNS + 1):
        reply = chat(model, messages, TOOLS)
        msg = reply.get("message") or {}
        text = msg.get("content") or ""
        calls = msg.get("tool_calls") or []

        if not calls:
            break

        messages.append({
            "role": "assistant",
            "content": text,
            "tool_calls": calls,
        })

        for call in calls:
            fn = (call.get("function") or {})
            name = fn.get("name") or ""
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    args = {}
            else:
                args = raw_args or {}

            handler = DISPATCH.get(name)
            if not handler:
                result = f"[error] unknown tool {name!r}"
            else:
                try:
                    result = handler(**args)
                except TypeError as exc:
                    result = f"[error] bad arguments for {name}: {exc}"
                except Exception as exc:  # noqa: BLE001
                    result = f"[error] {type(exc).__name__}: {exc}"

            sys.stderr.write(f"[local-llm] turn {turns} {name}: {result[:160].replace(chr(10), ' ')}\n")
            sys.stderr.flush()
            messages.append({
                "role": "tool",
                "tool_name": name,
                "content": result,
            })

    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "duration_ms": int((time.time() - started) * 1000),
        "num_turns": turns,
        "model": model,
        "engine": "ollama-local",
    }
    json.dump(envelope, sys.stdout, ensure_ascii=False)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
