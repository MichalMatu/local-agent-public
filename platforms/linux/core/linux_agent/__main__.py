"""Operator-only pilot entrypoint; no systemd installation side effects."""
from __future__ import annotations
import argparse
import json
import signal
from pathlib import Path

from linux_agent.agent import Agent, config_from_file, daemon_lock


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("action", choices=("once", "publish", "recover", "status", "disable", "serve"))
    args = p.parse_args()
    config = config_from_file(args.config)
    agent = Agent(config, args.state)
    if args.action == "disable":
        args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        from linux_agent.foundation.process import atomic_write_text
        atomic_write_text(args.state / "EMERGENCY_STOP", "disabled\n")
        out = {"status": "disabled"}
    elif args.action == "status":
        out = {"status": "disabled" if agent.disabled() else "enabled",
               "claimed": len(list((args.state / "claims").glob("*.json"))),
               "spooled": len(list((args.state / "results").glob("*.json")))}
    elif args.action == "once":
        out = agent.once()
    elif args.action == "serve":
        # Service lifetime is one daemon lock; SIGTERM stops new admission
        # and the active watcher cancels the current command to UNKNOWN.
        signal.signal(signal.SIGTERM, lambda _s, _f: agent.request_stop())
        signal.signal(signal.SIGINT, lambda _s, _f: agent.request_stop())
        agent.serve(poll_seconds=8.0, adaptive=True)
        out = {"status": "stopped"}
    else:
        with daemon_lock(args.state):
            if args.action == "recover":
                out = {"recovered_unknown": agent.recover()}
            else:
                out = {"published": agent.publish_pending()}
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
