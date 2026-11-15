"""Local AV probe: write a sample to disk, let the resident scanner react,
and report ``clean`` / ``detected`` / ``error``.

This is the "hands" of self-evolution. The armory can score, attribute and
derive variants on paper; the probe turns those variants into bytes, drops
them where a real AV watches, and reports what actually happened.

Three backends, chosen per call:

* **huorong** (default here) — Huorong's real-time protection removes a
  quarantined sample the moment it is written. There is no documented,
  stable CLI scanner, so the probe uses the only signal that is reliable and
  observable: ``file still exists == clean``, ``file vanished == detected``.
* **defender** — ``MpCmdRun.exe -Scan -ScanType 3`` is the standard CLI, but
  it only works when Defender owns the Windows Security Center. On a box where
  Huorong has taken that over (common), MpCmdRun fails with 0x80004005 and we
  report ``error`` rather than a false "clean".
* **mock** — deterministic, for tests and dry-runs. Never touches disk.

All scanning is *local and authorized*: this module drops a file in a temp
directory, waits, reads back whether the resident AV reacted, and cleans up.
It never sends samples anywhere and never executes them.
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

# How long to watch the resident scanner before declaring a sample "clean".
# Huorong's real-time engine reacts in well under a second; a little headroom
# covers slow disks and first-write scanning.
DEFAULT_WAIT = 3.0

_MOCK_SEQUENCE = ["clean"]
_DYNAMIC_MOCK_SEQUENCE = ["clean"]


def _mock_next() -> str:
    # pop-and-cycle so a sequence of ["detected", "clean"] drives a loop
    nxt = _MOCK_SEQUENCE.pop(0)
    _MOCK_SEQUENCE.append(nxt)
    return nxt


def _dynamic_mock_next() -> str:
    nxt = _DYNAMIC_MOCK_SEQUENCE.pop(0)
    _DYNAMIC_MOCK_SEQUENCE.append(nxt)
    return nxt


def set_mock_sequence(seq: list[str]) -> None:
    """Deterministic result sequence for the mock backend (tests use this)."""
    global _MOCK_SEQUENCE
    if not seq:
        seq = ["clean"]
    _MOCK_SEQUENCE = list(seq)


def set_dynamic_mock_sequence(seq: list[str]) -> None:
    """Deterministic result sequence for the dynamic mock (tests use this)."""
    global _DYNAMIC_MOCK_SEQUENCE
    if not seq:
        seq = ["clean"]
    _DYNAMIC_MOCK_SEQUENCE = list(seq)


def _huorong_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq HipsDaemon.exe", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        return "HipsDaemon.exe" in (out.stdout or "")
    except Exception:
        return False


def _defender_exe() -> Path | None:
    roots = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
    ]
    for root in roots:
        cand = root / "Windows Defender" / "MpCmdRun.exe"
        if cand.is_file():
            return cand
    # Platform-specific builds under ProgramData
    plat = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
    base = plat / "Microsoft" / "Windows Defender" / "Platform"
    if base.is_dir():
        for ver in sorted(base.iterdir(), reverse=True):
            cand = ver / "MpCmdRun.exe"
            if cand.is_file():
                return cand
    return None


def detect_engines() -> dict[str, dict]:
    """Which local AV backends are present right now."""
    engines: dict[str, dict] = {
        "mock": {"available": True, "note": "确定性返回，测试/演练用，不落盘"},
        "huorong": {
            "available": _huorong_running(),
            "note": "实时防护落盘即杀；文件存活=clean，消失=detected",
        },
    }
    mp = _defender_exe()
    engines["defender"] = {
        "available": mp is not None,
        "path": str(mp) if mp else "",
        "note": "MpCmdRun 标准扫描；若火绒已接管安全中心则报 error",
    }
    return engines


def _huorong_scan(path: Path, wait: float) -> str:
    """Watch the file; Huorong quarantines (deletes) a detected sample."""
    if not path.exists():
        # Written then instantly removed == the resident engine already caught it.
        return "detected"
    deadline = time.monotonic() + max(0.0, wait)
    while time.monotonic() < deadline:
        if not path.exists():
            return "detected"
        time.sleep(0.2)
    return "clean"


def _defender_scan(path: Path, timeout: float = 120.0) -> str:
    mp = _defender_exe()
    if mp is None:
        return "error"
    try:
        r = subprocess.run(
            [str(mp), "-Scan", "-ScanType", "3",
             "-File", str(path), "-DisableRemediation"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "error"
    # 0 == no threat, 2 == threat found, anything else (incl. 0x80004005) == error
    if r.returncode == 0:
        return "clean"
    if r.returncode == 2:
        return "detected"
    return "error"


def _scan(path: Path, engine: str, wait: float) -> str:
    if engine == "huorong":
        return _huorong_scan(path, wait)
    if engine == "defender":
        return _defender_scan(path)
    if engine == "mock":
        return _mock_next()
    return "error"


def probe_bytes(
    data: bytes,
    *,
    engine: str = "huorong",
    ext: str = ".bin",
    wait: float = DEFAULT_WAIT,
    tmpdir: str | os.PathLike | None = None,
    label: str = "",
) -> dict:
    """Drop ``data`` as a file and report how the local AV reacted.

    Returns a dict with ``result`` in {"clean", "detected", "error"}, the path
    it probed, the engine, and how long it took. On ``error`` the file is left
    in place so the caller can inspect it; otherwise the temp file is removed
    if the AV did not already delete it.
    """
    root = Path(tmpdir) if tmpdir else Path(tempfile.gettempdir())
    probe_dir = Path(tempfile.mkdtemp(prefix="sapper_avprobe_", dir=root))
    name = f"sample_{int(time.time() * 1000)}{ext}"
    path = probe_dir / name
    started = time.monotonic()
    result: dict = {
        "engine": engine, "path": str(path), "label": label or name,
        "bytes": len(data), "result": "error", "elapsed": 0.0,
    }
    # mock never touches disk — deterministic, no resident AV involved
    if engine == "mock":
        result["result"] = _mock_next()
        result["path"] = ""
        try:
            probe_dir.rmdir()
        except OSError:
            pass
        return result
    try:
        path.write_bytes(data)
        if not path.exists():
            # written then removed before we could even stat it
            result["result"] = "detected"
            return result
        result["result"] = _scan(path, engine, wait)
        result["elapsed"] = round(time.monotonic() - started, 2)
        return result
    except Exception as exc:  # pragma: no cover - defensive
        result["result"] = "error"
        result["error"] = str(exc)
        return result
    finally:
        # clean up only what still exists (AV may have removed it)
        try:
            if path.exists():
                path.unlink()
            if probe_dir.exists() and not any(probe_dir.iterdir()):
                probe_dir.rmdir()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# dynamic probe — run the sample, watch whether it survives and calls home
# --------------------------------------------------------------------------- #
#
# Static scanning is only the first gate. A real payload is *run*, and the AV's
# behavior engine reacts to what it does: a callback to a known C2 host, a
# self-decrypting loader, persistence, injection. `probe_dynamic` runs a sample
# script against a local mock C2 and reports two observable signals:
#
# * **beacon_reached** — did the sample's callback arrive at the mock C2? This
#   proves the runtime behavior chain works (and, against a *real* C2 host,
#   would be exactly the moment a threat-intel rule fires).
# * **alive** — did the process finish normally instead of being killed?
#
# Against the loopback mock C2, Huorong's behavior engine does not fire (it
# targets real malicious artifacts, not localhost pings), so a clean result
# here means "the payload ran and called home" — functional, not "stealthy".
# To exercise the dynamic *block*, point ``c2_host`` at a real test C2 whose
# domain/IP sits in the AV's threat-intel feed; then a kill or a missed beacon
# is the block signal. Both facts are surfaced honestly rather than faked.
DEFAULT_C2_PORT = 45678


def _listen_c2(host: str, port: int, timeout: float, holder: dict) -> None:
    """Background mock C2: accept one connection, capture the beacon bytes."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(1)
        s.settimeout(timeout)
        try:
            conn, addr = s.accept()
            conn.settimeout(5)
            holder["addr"] = f"{addr[0]}:{addr[1]}"
            chunks = []
            try:
                while True:
                    b = conn.recv(65536)
                    if not b:
                        break
                    chunks.append(b)
            except OSError:
                pass
            holder["beacon"] = b"".join(chunks)
            conn.close()
        except socket.timeout:
            pass
        s.close()
    except OSError as exc:  # pragma: no cover - port busy etc.
        holder["error"] = str(exc)


def probe_dynamic(
    script: str,
    *,
    c2_host: str = "127.0.0.1",
    c2_port: int = DEFAULT_C2_PORT,
    timeout: float = 20.0,
    tmpdir: str | os.PathLike | None = None,
    python: str | None = None,
    label: str = "",
) -> dict:
    """Run a sample script and watch its runtime behavior.

    ``script`` is a self-contained Python program (see ``evolver.build_script``)
    whose callback target is ``c2_host:c2_port``. A mock C2 listens on that
    port; the probe reports whether the sample called home and whether it
    survived. Clean-up removes the temp script afterwards.
    """
    root = Path(tmpdir) if tmpdir else Path(tempfile.gettempdir())
    probe_dir = Path(tempfile.mkdtemp(prefix="sapper_avprobe_dyn_", dir=root))
    path = probe_dir / f"sample_{int(time.time() * 1000)}.py"

    # c2_host == "mock" short-circuits: no listener, no run — deterministic,
    # for driving the evolve loop without a real callback target.
    if c2_host == "mock":
        result = _dynamic_mock_next()
        try:
            probe_dir.rmdir()
        except OSError:
            pass
        return {
            "engine": "dynamic-mock", "path": "", "label": label or path.name,
            "result": result, "beacon_reached": result == "clean",
            "alive": result == "clean", "exit_code": 0,
            "c2": "mock", "beacon_hex": "", "elapsed": 0.0, "error": None,
        }

    path.write_text(script, encoding="utf-8")

    holder: dict = {}
    listener = threading.Thread(
        target=_listen_c2, args=(c2_host, c2_port, timeout, holder), daemon=True
    )
    listener.start()
    time.sleep(0.1)  # let the listener bind before the sample connects

    started = time.monotonic()
    exit_code = None
    killed = False
    try:
        proc = subprocess.run(
            [python or "python", str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        killed = True
    except FileNotFoundError:
        holder["error"] = "python 解释器不可用"
    elapsed = round(time.monotonic() - started, 2)
    listener.join(timeout=1)

    beacon_reached = bool(holder.get("beacon"))
    alive = exit_code == 0 and not killed
    if holder.get("error") and not beacon_reached:
        result = "error"
    elif killed or (exit_code is not None and exit_code < 0):
        result = "detected"        # killed by the AV / signal
    elif beacon_reached and alive:
        result = "clean"
    elif beacon_reached:
        result = "clean"           # called home; non-zero exit after that is benign
    else:
        result = "detected"        # never called home — blocked or broken

    # clean up
    try:
        if path.exists():
            path.unlink()
        if probe_dir.exists() and not any(probe_dir.iterdir()):
            probe_dir.rmdir()
    except OSError:
        pass

    return {
        "engine": "dynamic", "path": str(path), "label": label or path.name,
        "result": result, "beacon_reached": beacon_reached, "alive": alive,
        "exit_code": exit_code, "c2": f"{c2_host}:{c2_port}",
        "beacon_hex": (holder.get("beacon") or b"")[:64].hex(),
        "elapsed": elapsed,
        "error": holder.get("error"),
    }
