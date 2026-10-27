"""Pure, read-only systemd USER unit planning for Raspberry Pi Zero 2 W.

Uses a pinned source tree containing BOTH independent Pi integration and
independent Linux runtime. This module never installs or starts a service.
"""
from __future__ import annotations


from linux_agent.service_plan import ReleasePair

UNIT_NAME = "local-agent-pi.service"
DEFAULT_MEMORY_MAX_MB = 320


def render_user_unit(
    pair: ReleasePair, *, candidate: bool, memory_max_mb: int = DEFAULT_MEMORY_MAX_MB
) -> str:
    """Render an exact-source candidate or rollback; zero filesystem effects."""
    pair.validate()
    if type(memory_max_mb) is not int or not 128 <= memory_max_mb <= 384:
        raise ValueError("Pi Zero 2 W service memory cap must be 128..384 MiB")
    root, sha = ((pair.candidate, pair.candidate_sha) if candidate
                 else (pair.rollback, pair.rollback_sha))
    pi_pkg = root / "platforms/raspberry-pi/local-agent/pi_agent/__main__.py"
    linux_pkg = root / "platforms/linux/core/linux_agent/__main__.py"
    if not pi_pkg.is_file() or pi_pkg.is_symlink() or not linux_pkg.is_file():
        raise ValueError("pinned release missing independent Pi or Linux runtime")
    pi_source = root / "platforms/raspberry-pi/local-agent"
    linux_source = root / "platforms/linux/core"
    linux_adapters = root / "platforms/linux/local-agent"
    # ReleasePair.validate enforces safe absolute paths and full SHA roots.
    return (
        "[Unit]\n"
        f"Description=Local Agent Pi Zero 2 W ({sha[:12]})\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "StartLimitIntervalSec=180\n"
        "StartLimitBurst=3\n"
        "\n[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={root}\n"
        f"Environment=PYTHONPATH={pi_source}:{linux_adapters}:{linux_source}\n"
        "Environment=PYTHONDONTWRITEBYTECODE=1\n"
        "Environment=LOCAL_AGENT_MEMORY_LIMIT_MB_MIN=1\n"
        "Environment=LOCAL_AGENT_MEMORY_LIMIT_MB_DEFAULT=96\n"
        f"Environment=LOCAL_AGENT_MEMORY_LIMIT_MB_MAX={memory_max_mb}\n"
        f"ExecStartPre={pair.python} -B -m pi_agent probe\n"
        f"ExecStart={pair.python} -B -m pi_agent --config {pair.config} --state {pair.state} serve\n"
        "UMask=0077\n"
        "KillMode=control-group\n"
        "TimeoutStopSec=40\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        f"MemoryMax={memory_max_mb}M\n"
        "TasksMax=32\n"
        "\n[Install]\n"
        "WantedBy=default.target\n"
    )


def plan_release_pair(pair: ReleasePair) -> dict[str, str]:
    """Do not conflate unit text with installation or physical acceptance."""
    return {
        "status": "plan_only_not_installed",
        "unit_name": UNIT_NAME,
        "candidate_sha": pair.candidate_sha,
        "rollback_sha": pair.rollback_sha,
        "candidate_unit": render_user_unit(pair, candidate=True),
        "rollback_unit": render_user_unit(pair, candidate=False),
    }
