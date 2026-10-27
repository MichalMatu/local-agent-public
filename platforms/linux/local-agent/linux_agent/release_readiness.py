"""Read-only, exact-SHA readiness gate for two independent Linux releases.

No installation, unit mutation, Git transport update, task execution or STOP
removal happens here. The check probes both released entrypoints using the
same Python interpreter and private, STOPPED installation identity.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from linux_agent.agent import config_from_file, validate_config
from linux_agent.release_controller import ServiceSwitchError, verify_clean_exact_source
from linux_agent.service_plan import ReleasePair, source_layout, source_pythonpath


_IMPORT_PROBE = """import json
from pathlib import Path
import linux_agent
import linux_agent.__main__
import linux_agent.agent
import linux_agent.runtime.core
import linux_agent.git_transport
print(json.dumps({"package": str(Path(linux_agent.__file__).resolve()),
                  "entrypoint": str(Path(linux_agent.__main__.__file__).resolve())}))
"""


def _run_probe(pair: ReleasePair, root: Path, *args: str) -> str:
    env = {**os.environ, "PYTHONPATH": source_pythonpath(root),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    try:
        result = subprocess.run(
            [str(pair.python), "-B", *args],
            cwd=root, env=env, check=False, capture_output=True, text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceSwitchError("independent Linux release probe unavailable") from exc
    if result.returncode != 0:
        # Never emit a release's stderr: it may include private paths/config.
        raise ServiceSwitchError("independent Linux release probe failed")
    if len(result.stdout) > 16384:
        raise ServiceSwitchError("independent Linux release probe oversized")
    return result.stdout.strip()


def _json_object(text: str, label: str) -> dict[str, object]:
    try:
        value = json.loads(text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ServiceSwitchError("invalid " + label + " probe JSON") from exc
    if not isinstance(value, dict):
        raise ServiceSwitchError("invalid " + label + " probe shape")
    return value


def _probe_release(pair: ReleasePair, root: Path, sha: str) -> dict[str, object]:
    adapter, entrypoint = source_layout(root)
    source = adapter / "linux_agent"
    imported = _json_object(_run_probe(pair, root, "-c", _IMPORT_PROBE), "import")
    if imported != {
        "package": str((source / "__init__.py").resolve()),
        "entrypoint": str(entrypoint.resolve()),
    }:
        raise ServiceSwitchError("release imports do not belong to exact source root")
    help_text = _run_probe(pair, root, "-m", "linux_agent", "--help")
    if "serve" not in help_text or "status" not in help_text:
        raise ServiceSwitchError("release command entrypoint missing serve/status")
    status = _json_object(_run_probe(
        pair, root, "-m", "linux_agent",
        "--config", str(pair.config), "--state", str(pair.state), "status",
    ), "status")
    if status.get("status") != "disabled":
        raise ServiceSwitchError("release is not respecting active EMERGENCY_STOP")
    for key in ("claimed", "spooled"):
        if type(status.get(key)) is not int or status[key] < 0:
            raise ServiceSwitchError("invalid read-only release status counts")
    return {"sha": sha, "imports_exact_source": True,
            "cli_serve_present": True, "stop_enforced": True,
            "claimed": status["claimed"], "spooled": status["spooled"]}


def inspect_pair(pair: ReleasePair) -> dict[str, object]:
    """Verify two *runnable* releases, without marking either as accepted."""
    pair.validate()
    for root in (pair.candidate, pair.rollback):
        verify_clean_exact_source(root, pair.candidate_sha if root == pair.candidate
                                  else pair.rollback_sha)
    for root in (pair.candidate, pair.rollback):
        # Keep state/config/interpreter outside a replaceable release tree.
        for path in (pair.state, pair.config, pair.python):
            if path == root or path.is_relative_to(root):
                raise ServiceSwitchError("persistent installation paths inside release")
    validate_config(config_from_file(pair.config), pair.state)
    if (pair.state / "EMERGENCY_STOP").is_symlink() or not (
        pair.state / "EMERGENCY_STOP"
    ).is_file():
        raise ServiceSwitchError("EMERGENCY_STOP required for release readiness")
    candidate = _probe_release(pair, pair.candidate, pair.candidate_sha)
    rollback = _probe_release(pair, pair.rollback, pair.rollback_sha)
    if (candidate["claimed"], candidate["spooled"]) != (
        rollback["claimed"], rollback["spooled"]
    ):
        raise ServiceSwitchError("release status reads disagree")
    return {
        "status": "two_runnable_releases_not_installed",
        "candidate": candidate, "rollback": rollback,
        "activation": "not_performed",
        "stop": "preserved",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", required=True, type=Path)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--rollback-root", required=True, type=Path)
    parser.add_argument("--rollback-sha", required=True)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    args = parser.parse_args(argv)
    pair = ReleasePair(
        candidate=args.candidate_root, candidate_sha=args.candidate_sha,
        rollback=args.rollback_root, rollback_sha=args.rollback_sha,
        python=Path(sys.executable).resolve(), config=args.config, state=args.state,
    )
    try:
        result = inspect_pair(pair)
    except (OSError, ValueError, ServiceSwitchError) as exc:
        print(f"Linux release readiness REFUSED: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
