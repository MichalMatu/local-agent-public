"""Remote exact-entry atomic commit operations with truthful partial effects."""

from __future__ import annotations

from typing import TYPE_CHECKING

from local_agent.host_ops.core.config import HostTarget
from local_agent.host_ops.core.execution import ProcessState

from .client import SshClient
from .errors import SshTransferError
from .remote_probes import _process_failure, _remote_test

if TYPE_CHECKING:
    from .transfer import _TransferBudget


def _commit_remote_no_clobber(
    client: SshClient,
    target: HostTarget,
    stage: str,
    destination: str,
    budget: _TransferBudget,
) -> None:
    # GNU ln -T treats the destination as an exact entry, including when a
    # directory/symlink appears after preflight. Creating this hard link is an
    # atomic no-replace operation on the same filesystem.
    result = client.execute(
        target, ("ln", "-T", "--", stage, destination),
        limits=budget.remaining(),
    )
    if result.ok:
        return
    if result.state is ProcessState.SPAWN_FAILED:
        raise SshTransferError(
            _process_failure("remote no-clobber hard-link commit", result),
            action_attempted=False,
            code="ssh_process_spawn_failed",
            phase="remote_commit",
            process=result,
        )
    if result.state is not ProcessState.COMPLETED or result.exit_code == 255:
        raise SshTransferError(
            _process_failure("remote no-clobber hard-link commit", result),
            action_attempted=True,
            commit_uncertain=True,
            code="ssh_commit_uncertain",
            phase="remote_commit",
            process=result,
        )
    # This post-failure probe is for diagnostics only: it is never used to
    # authorize a later unsafe check-then-move fallback.
    if (
        _remote_test(client, target, "-L", destination, budget)
        or _remote_test(client, target, "-e", destination, budget)
    ):
        raise SshTransferError(
            "remote destination appeared during no-clobber commit",
            action_attempted=True,
            code="ssh_destination_conflict",
            phase="remote_commit",
            process=result,
        )
    raise SshTransferError(
        _process_failure("remote exact-target hard link unavailable", result),
        action_attempted=True,
        code="ssh_remote_capability_unsupported",
        phase="remote_commit",
        process=result,
    )


def _commit_remote_replace(
    client: SshClient,
    target: HostTarget,
    stage: str,
    destination: str,
    budget: _TransferBudget,
) -> None:
    # GNU mv -T replaces only the exact destination entry. In particular,
    # it does not put the stage inside a newly appearing directory/symlink.
    result = client.execute(
        target, ("mv", "-T", "--", stage, destination),
        limits=budget.remaining(),
    )
    if result.ok:
        return
    if result.state is ProcessState.SPAWN_FAILED:
        raise SshTransferError(
            _process_failure("remote exact-target replacement", result),
            action_attempted=False,
            code="ssh_process_spawn_failed",
            phase="remote_commit",
            process=result,
        )
    if result.state is not ProcessState.COMPLETED or result.exit_code == 255:
        raise SshTransferError(
            _process_failure("remote exact-target replacement", result),
            action_attempted=True,
            commit_uncertain=True,
            code="ssh_commit_uncertain",
            phase="remote_commit",
            process=result,
        )
    if (
        _remote_test(client, target, "-d", destination, budget)
        and not _remote_test(client, target, "-L", destination, budget)
    ):
        raise SshTransferError(
            "remote destination became a directory during replacement",
            action_attempted=True,
            code="ssh_destination_conflict",
            phase="remote_commit",
            process=result,
        )
    raise SshTransferError(
        _process_failure("remote exact-target replacement unsupported or rejected", result),
        action_attempted=True,
        code="ssh_remote_capability_unsupported",
        phase="remote_commit",
        process=result,
    )
