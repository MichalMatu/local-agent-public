from __future__ import annotations

import local_agent.foundation.storage as storage
import local_agent.daemon.service as agentd
from local_agent.repository.context import RepositoryContext
from local_agent.supervisor.host_control import HostControlSource


def sync_control_quietly() -> None:
    "Run bounded routine control-branch sync without low-level Git output."
    storage.sync_control(agentd.core, tasks_dir=agentd.REMOTE_TASKS_DIR)


def bind_supervisor_control(
    repository: RepositoryContext | HostControlSource,
    *,
    daemon_version: str,
) -> None:
    """Bind either the legacy project control or the host's own private control.

    Host mode uses a new per-installation supervisor namespace. It never
    interprets an old project's daemon control request as host authority.
    """
    agentd.core.CONTROL = repository.control
    agentd.core.CONTROL_BRANCH = repository.control_branch
    if isinstance(repository, HostControlSource):
        agentd.configure_task_mailbox()
        # Never run legacy queue GC or repair project task paths in the
        # machine-owned control checkout.
        agentd.REMOTE_TASKS_DIR = f"{repository.namespace}/tasks"
        agentd.REMOTE_RESULTS_DIR = f"{repository.namespace}/results"
        agentd.REMOTE_RUNS_DIR = f"{repository.namespace}/runs"
        agentd.REMOTE_DAEMON_STATUS = repository.status_path
        agentd.REMOTE_CONTROL_REQUEST = repository.request_path
        agentd.REMOTE_CONTROL_ACK_DIR = repository.ack_directory
    else:
        agentd.configure_task_mailbox(
            repository.host_id, enabled=repository.host_mailbox_enabled,
        )
    agentd.DAEMON_VERSION = daemon_version
