"""Linux Host Ops strict OpenSSH check/exec for one explicitly chosen remote.

No enrollment, implicit host-key trust, password prompts, tunnelling, scanning,
remote repository scheduling or automatic host selection.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import selectors
import shlex
import stat
import subprocess
import time

from linux_agent.host_ops_artifacts import _parent
from linux_agent.host_ops_network import _target

_SSH = "/usr/bin/ssh"
_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_MAX_OUTPUT = 32_768


class SSHOperationError(ValueError):
    """A remote target, credential boundary or SSH result was not accepted."""


def _file(path: str, *, private: bool) -> None:
    with _parent(path) as (parent, name):
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise SSHOperationError("SSH identity and known_hosts must be regular files")
        if private and (info.st_mode & 0o077):
            raise SSHOperationError("SSH private key must not be group/world accessible")


def command_argv(host: str, user: str, identity: str, known_hosts: str,
                 remote_argv: tuple[str, ...], *, port: int = 22,
                 timeout: float = 20.0) -> list[str]:
    _target(host, min(timeout, 30.0))
    if not _USER.fullmatch(user):
        raise SSHOperationError("invalid explicit SSH username")
    if type(port) is not int or not 1 <= port <= 65535:
        raise SSHOperationError("SSH port must be an integer 1..65535")
    if not 0 < timeout <= 120:
        raise SSHOperationError("SSH timeout must be in (0, 120] seconds")
    if not 1 <= len(remote_argv) <= 64 or any(
        not isinstance(arg, str) or not arg or "\x00" in arg or len(arg) > 4096
        for arg in remote_argv
    ):
        raise SSHOperationError("remote argv requires 1..64 finite nonempty arguments")
    if sum(len(arg) for arg in remote_argv) > 32768:
        raise SSHOperationError("remote argv exceeds 32768 characters")
    if not Path(_SSH).is_file():
        raise SSHOperationError("system OpenSSH executable unavailable")
    try:
        _file(identity, private=True)
        _file(known_hosts, private=False)
    except OSError as exc:
        raise SSHOperationError("explicit SSH identity or known_hosts unavailable") from exc
    connect_seconds = min(10, max(1, int(timeout)))
    options = (
        "IdentitiesOnly=yes", "BatchMode=yes", "PreferredAuthentications=publickey",
        "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
        "GSSAPIAuthentication=no", "StrictHostKeyChecking=yes",
        "GlobalKnownHostsFile=/dev/null", f"UserKnownHostsFile={known_hosts}",
        "UpdateHostKeys=no", "ForwardAgent=no", "ClearAllForwardings=yes",
        "PermitLocalCommand=no", "CanonicalizeHostname=no",
        "ProxyCommand=none", "ProxyJump=none", "ControlMaster=no",
        "ControlPath=none", "ControlPersist=no", "RequestTTY=no",
        "NumberOfPasswordPrompts=0", "ConnectionAttempts=1",
        f"ConnectTimeout={connect_seconds}", "LogLevel=ERROR",
    )
    return [
        _SSH, "-F", "/dev/null", "-T", "-i", identity, "-p", str(port),
        *[token for option in options for token in ("-o", option)],
        "-l", user, "--", host, shlex.join(remote_argv),
    ]


def _execute(argv: list[str], timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, close_fds=True)
    sel = selectors.DefaultSelector()
    out, err = bytearray(), bytearray()
    try:
        assert proc.stdout is not None and proc.stderr is not None
        sel.register(proc.stdout, selectors.EVENT_READ, out)
        sel.register(proc.stderr, selectors.EVENT_READ, err)
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SSHOperationError("SSH whole-operation timeout; remote effect uncertain")
            for key, _ in sel.select(min(remaining, 0.2)):
                block = os.read(key.fileobj.fileno(), 4096)
                if not block:
                    sel.unregister(key.fileobj)
                    continue
                key.data.extend(block)
                if len(out) + len(err) > _MAX_OUTPUT:
                    raise SSHOperationError("SSH combined output exceeded bounded limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SSHOperationError("SSH whole-operation timeout; remote effect uncertain")
        exit_code = proc.wait(timeout=remaining)
        return {"exit_code": exit_code, "ok": exit_code == 0,
                "stdout": bytes(out).decode("utf-8", errors="replace"),
                "stderr": bytes(err).decode("utf-8", errors="replace")}
    except subprocess.TimeoutExpired as exc:
        raise SSHOperationError("SSH whole-operation timeout; remote effect uncertain") from exc
    finally:
        sel.close()
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


def ssh_operation(action: str, host: str, user: str, identity: str,
                  known_hosts: str, *, remote_argv: tuple[str, ...] = (),
                  port: int = 22, timeout: float = 20.0) -> dict:
    if action not in {"check", "exec"}:
        raise SSHOperationError("unsupported SSH operation")
    if action == "check":
        if remote_argv:
            raise SSHOperationError("SSH check does not accept a custom command")
        remote_argv = ("printf", "LA_SSH_OK")
    argv = command_argv(host, user, identity, known_hosts, remote_argv,
                        port=port, timeout=timeout)
    try:
        result = _execute(argv, timeout)
    except OSError as exc:
        raise SSHOperationError("SSH invocation failed") from exc
    if action == "check" and (not result["ok"] or result["stdout"].strip() != "LA_SSH_OK"):
        result["ok"] = False
        result["identity_check_failed"] = True
    return {"host": host, "user": user, "port": port, "action": action,
            "strict_host_key": True, "passwordless": True, **result}
