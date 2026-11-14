#!/usr/bin/env python3
"""Windows launcher for sapper.

sapper's only non-portable dependency is `import fcntl` + `fcntl.flock()` for
its project-lock (lib/sapper.py, Graph.locked). This launcher injects a
stdlib-only shim into sys.modules BEFORE sapper is executed, so the upstream
source tree stays untouched (and the Linux/Docker image keeps using the real
fcntl).

Lock semantics: the lock region is byte 0..1 of the lock file, so every
participant that goes through this shim sees the same region.
"""
from __future__ import annotations

import os
import runpy
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
ENTRY = os.path.join(LIB, "sapper.py")


def install_fcntl_shim() -> None:
    if sys.platform != "win32" or "fcntl" in sys.modules:
        return

    import msvcrt
    import time

    mod = types.ModuleType("fcntl")
    mod.__doc__ = "Windows shim: flock() emulated with msvcrt.locking()."
    mod.LOCK_SH = 0x01
    mod.LOCK_EX = 0x02
    mod.LOCK_NB = 0x04
    mod.LOCK_UN = 0x08

    def flock(fd: int, operation: int) -> None:
        if operation & mod.LOCK_UN:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            return

        blocking = not (operation & mod.LOCK_NB)
        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if not blocking:
                    raise
                time.sleep(0.05)

    mod.flock = flock
    sys.modules["fcntl"] = mod


def main() -> None:
    install_fcntl_shim()
    if LIB not in sys.path:
        sys.path.insert(0, LIB)
    sys.argv[0] = "sapper"
    runpy.run_path(ENTRY, run_name="__main__")


if __name__ == "__main__":
    main()
