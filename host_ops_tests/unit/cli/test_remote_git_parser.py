from __future__ import annotations

from local_agent.host_ops.cli.main import build_parser

REVISION = "a" * 40
REPOSITORY_URL = "https://git.example.test/team/project.git"


def test_remote_git_prepare_parser() -> None:
    args = build_parser().parse_args(
        [
            "remote",
            "git",
            "prepare",
            "build-host",
            "--repo",
            REPOSITORY_URL,
            "--revision",
            REVISION,
            "--workspace",
            "project-cache",
            "--lock",
            "build-slot",
        ]
    )

    assert args.command == "remote"
    assert args.remote_command == "git"
    assert args.remote_git_command == "prepare"
    assert args.workspace_name == "project-cache"
    assert args.lock == "build-slot"


def test_remote_git_run_parser_preserves_remote_flags_after_separator() -> None:
    args = build_parser().parse_args(
        [
            "remote",
            "git",
            "run",
            "build-host",
            "--repo",
            REPOSITORY_URL,
            "--revision",
            REVISION,
            "--workspace",
            "project-cache",
            "--",
            "python3",
            "-m",
            "compileall",
            "src",
        ]
    )

    assert args.remote_git_command == "run"
    assert args.remote_argv == ["python3", "-m", "compileall", "src"]


def test_remote_git_run_current_defaults_to_cwd_and_origin() -> None:
    args = build_parser().parse_args(
        [
            "remote",
            "git",
            "run-current",
            "build-host",
            "--lock",
            "build-slot",
            "--",
            "make",
            "check",
        ]
    )

    assert args.remote_git_command == "run-current"
    assert args.source_path == "."
    assert args.remote_name == "origin"
    assert args.repository_url_override is None
    assert args.workspace_name is None
    assert args.lock == "build-slot"
    assert args.remote_argv == ["make", "check"]


def test_remote_git_prepare_current_accepts_repo_path_remote_and_worker_url() -> None:
    args = build_parser().parse_args(
        [
            "remote",
            "git",
            "prepare-current",
            "build-host",
            "--repo-path",
            "/work/project",
            "--remote",
            "upstream",
            "--repo-url",
            "https://worker.example.test/team/project.git",
            "--workspace",
            "custom-cache",
        ]
    )

    assert args.remote_git_command == "prepare-current"
    assert args.source_path == "/work/project"
    assert args.remote_name == "upstream"
    assert args.repository_url_override == "https://worker.example.test/team/project.git"
    assert args.workspace_name == "custom-cache"
