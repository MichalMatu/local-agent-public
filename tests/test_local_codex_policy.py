from __future__ import annotations

import unittest

from local_agent.runtime.task_contract import validate_task


class LocalCodexPolicyTests(unittest.TestCase):
    def test_allows_codex_as_search_data_or_filename(self) -> None:
        for command in (
            "rg codex README.md", "echo codex", "cat docs/codex.md",
            "printf '%s' 'codex exec example'", "echo ';' codex",
            "git diff -- codex", "python -c 'print(\"codex\")'",
            "echo '$(codex exec x)'", "bash -lc 'echo codex'",
            "cat <<'EOF'\ncodex exec example\n$(codex exec example)\nEOF\n",
            "cat <<EOF\ncodex exec example\nEOF\n",
        ):
            with self.subTest(command=command):
                validate_task({"id": "allowed-mention", "resources": [], "commands": [command]})

    def test_rejects_compound_wrapped_and_nested_invocations(self) -> None:
        for command in (
            "true && codex exec x", "echo x | codex exec x", "env X=1 codex exec x",
            "sudo -u user /usr/bin/codex exec x", "timeout 30 codex exec x",
            "bash -lc 'codex exec x'", "pnpm dlx @openai/codex exec x",
            "echo $(codex exec x)", "'codex' exec x",
            "true\ncodex exec x", "true # comment\ncodex exec x",
            'echo "$(codex exec x)"', "echo `codex exec x`",
            "cat <<EOF\n$(codex exec x)\nEOF\n",
        ):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "may not invoke local Codex"):
                validate_task({"id": "blocked-invocation", "resources": [], "commands": [command]})

    def test_rejects_codex_in_legacy_commands(self) -> None:
        task = {
            "id": "reject-codex-command",
            "resources": [],
            "commands": ["codex exec --full-auto 'implement the change'"],
        }
        with self.assertRaisesRegex(ValueError, "may not invoke local Codex"):
            validate_task(task)

    def test_rejects_absolute_codex_path_in_structured_steps(self) -> None:
        task = {
            "id": "reject-codex-step",
            "resources": [],
            "steps": [
                {
                    "name": "delegate",
                    "command": "/opt/homebrew/bin/codex exec 'run tests and fix failures'",
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "may not invoke local Codex"):
            validate_task(task)

    def test_rejects_npx_codex_in_verify_commands(self) -> None:
        task = {
            "id": "reject-npx-codex",
            "resources": [],
            "verify_commands": ["npx @openai/codex exec 'review the result'"],
        }
        with self.assertRaisesRegex(ValueError, "may not invoke local Codex"):
            validate_task(task)


if __name__ == "__main__":
    unittest.main()
