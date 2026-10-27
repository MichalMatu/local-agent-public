"""Bounded read-only local Git context for the Debian Host Ops CLI."""
from __future__ import annotations

import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import time

_SHA = re.compile(r"^[a-f0-9]{40}(?:[a-f0-9]{24})?$")
_MAX_OUTPUT = 65_536


class GitInspectionError(ValueError):
    """Unsafe, unavailable, non-repository or over-budget Git inspection."""


def _git(argv: tuple[str, ...], cwd: Path, deadline: float,
         *, output_limit: int = _MAX_OUTPUT) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise GitInspectionError("git executable unavailable")
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    proc = subprocess.Popen(
        [executable, "-C", str(cwd), *argv],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, close_fds=True,
    )
    streams = selectors.DefaultSelector()
    out, err = bytearray(), bytearray()
    try:
        assert proc.stdout is not None and proc.stderr is not None
        streams.register(proc.stdout, selectors.EVENT_READ, out)
        streams.register(proc.stderr, selectors.EVENT_READ, err)
        while streams.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitInspectionError("git inspection timed out")
            for key, _ in streams.select(min(remaining, 0.25)):
                block = os.read(key.fileobj.fileno(), 4096)
                if not block:
                    streams.unregister(key.fileobj)
                    continue
                target = key.data
                target.extend(block)
                if len(out) + len(err) > output_limit:
                    raise GitInspectionError("git output exceeds bounded limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitInspectionError("git inspection timed out")
        if proc.wait(timeout=remaining) != 0:
            raise GitInspectionError("git inspection command failed")
        try:
            return bytes(out).decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise GitInspectionError("git output is not valid UTF-8") from exc
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise GitInspectionError("git inspection unavailable or timed out") from exc
    finally:
        streams.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()


def inspect_git(path: str, *, timeout: float = 15.0,
                max_output: int = _MAX_OUTPUT) -> dict:
    if not isinstance(path, str) or not Path(path).is_absolute() or ".." in Path(path).parts:
        raise GitInspectionError("git inspection path must be explicit, absolute and without traversal")
    if not 0 < timeout <= 60:
        raise GitInspectionError("git inspection timeout must be in (0, 60] seconds")
    if type(max_output) is not int or not 1024 <= max_output <= _MAX_OUTPUT:
        raise GitInspectionError("max_output must be between 1024 and 65536 bytes")
    root = Path(path)
    if not root.is_dir():
        raise GitInspectionError("git inspection directory unavailable")
    deadline = time.monotonic() + timeout
    try:
        discovered = _git(("rev-parse", "--show-toplevel"), root, deadline, output_limit=max_output)
        canonical = Path(discovered).resolve(strict=True)
        revision = _git(("rev-parse", "--verify", "HEAD^{commit}"),
                        canonical, deadline, output_limit=max_output)
        if not _SHA.fullmatch(revision):
            raise GitInspectionError("git HEAD is not a verified hexadecimal commit")
        branch = _git(("rev-parse", "--abbrev-ref", "HEAD"), canonical, deadline,
                      output_limit=max_output)
        status = _git(("status", "--porcelain=v1", "--untracked-files=normal"),
                      canonical, deadline, output_limit=max_output)
    except OSError as exc:
        raise GitInspectionError("could not resolve Git repository") from exc
    return {
        "root": str(canonical),
        "head": revision,
        "branch": None if branch == "HEAD" else branch,
        "dirty": bool(status),
        "status_entry_count": len(status.splitlines()) if status else 0,
        "status_paths_exposed": False,
        "git_optional_locks": False,
    }
