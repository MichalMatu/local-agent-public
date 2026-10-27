"""Run the GitHub-first Host Ops transport contract under the coverage gate.

The end-to-end smoke lives with Conversation Fabric unit tests; the Host Ops
coverage gate must exercise the same real mailbox and HTTP adapter instead of
misreporting the new adapter as completely untested.
"""

from tests.test_github_first_local_bridge import GithubFirstBridgeTests as TestChatBridgeTransport

__all__ = ["TestChatBridgeTransport"]
