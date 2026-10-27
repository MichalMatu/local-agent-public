# Tasks and everyday use

After [installation](../INSTALL_WITH_CODEX.md), choose the exact machine and
ask ChatGPT to publish a task through your connected private Git repository.
The installed Agent fetches its own namespace and publishes a durable result.

## Examples

| Request | Typical use |
| --- | --- |
| Check free disk space and the status of my service on Debian | Server diagnostics |
| Run the tests for my registered project on my Mac | Development in an existing local toolchain |
| Build this project on the selected machine and report the output | Local builds |
| Show bounded diagnostics from my Pi | Device inspection |
| Run a short command in Termux and return its result | Phone-side command tasks |

Platform capabilities differ; see [support](SUPPORT.md). Tools and project
access must exist on that machine. Projects are optional and are added through
explicit [repository onboarding](REPOSITORY_ONBOARDING.md).

## Task lifecycle

1. Select the exact host ID and installation ID from your private receipt.
2. Publish a new immutable task ID with bounded commands and resources.
3. Read status and the terminal result from that installation's Git namespace.
4. Preserve the result and task ID. Publication retry reuses a saved result.

A Git commit containing a request does not prove execution. A local result
does not prove remote delivery. Read the authoritative result after publication.
If effects are uncertain, retain UNKNOWN and inspect before deciding on a new
request. Never automatically replay a command to repair a missing response.

Status and CPU/RAM/disk inspection happen on request. There is no automatic
machine selection, failover or cross-host scheduler.

[Wire contract and JSON example](HOST_CONTROL_PLANE.md) ·
[operations](OPERATIONS.md) · [FAQ](FAQ.md).
