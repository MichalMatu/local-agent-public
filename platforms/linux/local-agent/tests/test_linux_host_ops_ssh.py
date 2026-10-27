"""Offline no-network OpenSSH boundary checks; never contact the real Mac."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from linux_agent.host_ops_ssh import SSHOperationError, command_argv, ssh_operation


class SSHTests(unittest.TestCase):
    def test_strict_flags_and_shell_quoted_remote_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            key = root / "key"
            known = root / "known_hosts"
            key.write_text("disposable fixture")
            key.chmod(0o600)
            known.write_text("disposable fixture")
            argv = command_argv("127.0.0.1", "admin", str(key), str(known),
                                ("printf", "%s", "argument with spaces; $(bad)"),
                                timeout=40)
            flat = " ".join(argv)
            for required in ("-F /dev/null", "StrictHostKeyChecking=yes",
                             "IdentitiesOnly=yes", "BatchMode=yes",
                             "PasswordAuthentication=no", "ProxyCommand=none",
                             "ForwardAgent=no", "UserKnownHostsFile=",
                             "GlobalKnownHostsFile=/dev/null"):
                self.assertIn(required, flat)
            self.assertIn("'argument with spaces; $(bad)'", argv[-1])
            self.assertEqual(argv[-2], "127.0.0.1")
            with mock.patch("linux_agent.host_ops_ssh._execute",
                            return_value={"exit_code": 0, "ok": True,
                                          "stdout": "LA_SSH_OK", "stderr": ""}) as runner:
                result = ssh_operation("check", "127.0.0.1", "admin",
                                       str(key), str(known))
            self.assertTrue(result["strict_host_key"])
            self.assertTrue(result["ok"])
            self.assertIn("printf LA_SSH_OK", runner.call_args.args[0][-1])


    def test_ssh_check_does_not_accept_wrong_remote_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            key, known = root / "key", root / "known"
            key.write_text("fixture")
            key.chmod(0o600)
            known.write_text("fixture")
            with mock.patch("linux_agent.host_ops_ssh._execute",
                            return_value={"exit_code": 0, "ok": True,
                                          "stdout": "unexpected", "stderr": ""}):
                response = ssh_operation("check", "localhost", "admin",
                                         str(key), str(known))
            self.assertFalse(response["ok"])
            self.assertTrue(response["identity_check_failed"])


    def test_missing_bad_key_symlink_user_and_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            key = root / "key"
            known = root / "known"
            key.write_text("not a real credential")
            known.write_text("not a real known-host record")
            key.chmod(0o644)
            def invoke(host="localhost", user="admin", identity=str(key)):
                return command_argv(host, user, identity, str(known), ("true",))
            with self.assertRaisesRegex(SSHOperationError, "private key"):
                invoke()
            key.chmod(0o600)
            for host, user in (("-danger", "admin"), ("localhost; rm", "admin"),
                               ("localhost", "-oProxyCommand=bad")):
                with self.subTest(host=host, user=user), self.assertRaises(ValueError):
                    invoke(host, user)
            link = root / "key_link"
            link.symlink_to(key)
            with self.assertRaises((SSHOperationError, OSError)):
                invoke(identity=str(link))
            with self.assertRaises(SSHOperationError):
                invoke(identity=str(root / "missing"))

    def test_bad_args_and_no_implicit_remote_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            key = root / "key"
            known = root / "known"
            key.write_text("fixture")
            key.chmod(0o600)
            known.write_text("fixture")
            common = ("localhost", "admin", str(key), str(known))
            with self.assertRaises(SSHOperationError):
                command_argv(*common, (), port=0)
            with self.assertRaises(SSHOperationError):
                command_argv(*common, ())
            with self.assertRaises(SSHOperationError):
                ssh_operation("check", *common, remote_argv=("whoami",))
            with self.assertRaises(SSHOperationError):
                ssh_operation("not_an_action", *common)


if __name__ == "__main__":
    unittest.main()
