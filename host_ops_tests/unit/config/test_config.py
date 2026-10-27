from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.host_ops.core.config import (
    ConfigError,
    ConfigSecurityError,
    HostOpsConfig,
    HostTarget,
    config_file_path,
    load_config,
    parse_config,
)


def test_parse_config_loads_host_target() -> None:
    config = parse_config(
        {
            "version": 1,
            "hosts": {
                "termux-phone": {
                    "host": "192.0.2.100",
                    "port": 8022,
                    "user": "u0_a123",
                    "identity_file": "~/.ssh/host_ops_termux_ed25519",
                    "known_hosts_file": "~/.ssh/known_hosts_phone",
                }
            },
        }
    )

    target = config.hosts["termux-phone"]
    assert target.host == "192.0.2.100"
    assert target.port == 8022
    assert target.user == "u0_a123"
    assert target.identity_file == "~/.ssh/host_ops_termux_ed25519"
    assert target.known_hosts_file == "~/.ssh/known_hosts_phone"


@pytest.mark.parametrize("key", ["password", "token", "private_key", "api_key"])
def test_parse_config_rejects_credential_like_fields(key: str) -> None:
    with pytest.raises(ConfigSecurityError):
        parse_config({"hosts": {"phone": {"host": "example", key: "do-not-store"}}})


def test_parse_config_rejects_unknown_fields() -> None:
    with pytest.raises(ConfigError, match="unknown field"):
        parse_config({"hosts": {"phone": {"host": "example", "magic": True}}})


@pytest.mark.parametrize(
    "host",
    [
        "-Fmalicious-config",
        "user@example.test",
        "example.test with-space",
        "example.test\nProxyCommand=evil",
    ],
)
def test_host_target_rejects_values_that_could_change_ssh_parsing(host: str) -> None:
    with pytest.raises(ValueError):
        HostTarget(alias="phone", host=host)


@pytest.mark.parametrize(
    "user",
    [
        "-oProxyCommand=evil",
        "user@example",
        "user name",
        "user\nProxyCommand=evil",
    ],
)
def test_host_target_rejects_unsafe_remote_user_values(user: str) -> None:
    with pytest.raises(ValueError):
        HostTarget(alias="phone", host="example.test", user=user)


@pytest.mark.parametrize(
    "identity_file",
    [
        "relative/key",
        "-malicious-option",
        "/tmp/key\nProxyCommand=evil",
        "/tmp/key\x7f",
        "/tmp/key-%h",
        "${HOME}/.ssh/key",
        "",
    ],
)
def test_host_target_rejects_unsafe_identity_file_paths(identity_file: str) -> None:
    with pytest.raises(ValueError):
        HostTarget(alias="phone", host="example.test", identity_file=identity_file)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("alias", None),
        ("host", None),
        ("user", 123),
        ("identity_file", 123),
    ],
)
def test_host_target_rejects_invalid_direct_model_types(field: str, value) -> None:
    arguments = {"alias": "phone", "host": "example.test", "user": "user"}
    arguments[field] = value

    with pytest.raises(ValueError):
        HostTarget(**arguments)


def test_host_ops_config_rejects_boolean_version() -> None:
    with pytest.raises(ValueError):
        HostOpsConfig(version=True)


@pytest.mark.parametrize(
    "value",
    [123, "", "relative", "-option", "/tmp/%h", "/tmp/$HOME", '/tmp/a" b',
     "/tmp/a\\b", "/tmp/a\nProxyCommand=evil", "/tmp/a\x7f"],
)
def test_config_rejects_unsafe_known_hosts_paths(value) -> None:
    with pytest.raises(ConfigError, match="known_hosts_file"):
        parse_config({"hosts": {"debian": {"host": "example.test", "known_hosts_file": value}}})


def test_load_config_returns_defaults_when_file_missing(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml")

    assert config.version == 1
    assert config.hosts == {}


def test_load_config_wraps_toml_errors(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[hosts.phone\nhost='broken'", encoding="utf-8")

    with pytest.raises(ConfigError, match="could not load"):
        load_config(path)


def test_config_path_prefers_explicit_environment_override(tmp_path: Path) -> None:
    explicit = tmp_path / "custom.toml"

    assert config_file_path(env={"HOST_OPS_CONFIG": str(explicit)}, home=tmp_path) == explicit


def test_config_path_uses_xdg_config_home(tmp_path: Path) -> None:
    assert config_file_path(env={"XDG_CONFIG_HOME": str(tmp_path)}, home=Path("/ignored")) == (
        tmp_path / "host-ops" / "config.toml"
    )
