# Security policy

## Supported versions

The latest published 4.20.x release is the supported public source line.
Older release tags remain available for audit, but do not imply active support.

## Report a vulnerability

Please use GitHub's **Report a vulnerability** option on this repository's
**Security** tab, if private vulnerability reporting is enabled:
https://github.com/MichalMatu/local-agent-public/security/advisories/new

If this option is unavailable, request a private contact channel from the
maintainer without disclosing the vulnerability or any credentials in public.
Do not post exploitation details, tokens, commands from private task inboxes,
or customer data in public issues or pull requests.

Include the affected version or commit, platform, minimal sanitized
reproduction, security impact and any recommended mitigation. Never send
live credentials. No guaranteed response time is implied by this policy.

## Security boundaries

Local Agent executes commands using its service OS user's permissions; it
is **not a sandbox**. Writers allowed to the private Git task namespace are
trusted command publishers in `github_writers` mode. Keep the task/result
transport private and restrict write access to accounts you trust.

Never use this public source repository as a task inbox or publish local
installation receipts, keys or private output here. Interrupted or uncertain
effects are retained as UNKNOWN rather than automatically repeated.

See [the security model](docs/SECURITY_MODEL.md) and
[operations / STOP](docs/OPERATIONS.md).

## Release authenticity

New releases should have a signed annotated Git tag pinned to an exact source
commit and a locally verified signature from an independently trusted signing
identity. Verify archive checksums and supplied provenance separately.
See [release signing](docs/RELEASE_SIGNING.md).

Historical `v4.20.14` was published with an unsigned annotated tag; it is
not retroactively claimed to be signed. Never rewrite an existing public tag
to create the appearance of historical provenance.
