from __future__ import annotations

import hashlib
import json
import re
import secrets
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

from linux_agent.config import MEMORY_LIMITS, TIMEOUTS
from linux_agent.repository.binding import canonical_agent_binding

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_RESOURCE_RE = re.compile(r"^[a-z0-9._:-]+$")
_DEDUPE_KEY_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")
MAX_DEDUPE_KEY_CHARS = 200
DEFAULT_IDLE_TIMEOUT = TIMEOUTS.idle_default
MAX_IDLE_TIMEOUT = TIMEOUTS.idle_max
DEFAULT_TASK_TIMEOUT = TIMEOUTS.task_default
MAX_TASK_TIMEOUT = TIMEOUTS.task_max
TASK_FINALIZATION_RESERVE = 60
MIN_MEMORY_LIMIT_MB = MEMORY_LIMITS.minimum_mb
DEFAULT_MEMORY_LIMIT_MB = MEMORY_LIMITS.default_mb
MAX_MEMORY_LIMIT_MB = MEMORY_LIMITS.maximum_mb
MAX_TASK_RESOURCES = 8

MAX_TASK_FILE_BYTES = 4 * 1024 * 1024
MAX_TASK_LIST_ITEMS = 256
MAX_COMMAND_CHARS = 32_768
MAX_PATCH_BYTES = 2 * 1024 * 1024
MAX_WRITE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_WRITE_BYTES = 8 * 1024 * 1024
MAX_TASK_PATH_CHARS = 1024
TASK_PAYLOAD_REF_KEY = "payload_file"
MAX_TASK_PAYLOAD_FILES = 1536
MAX_TASK_PAYLOAD_BYTES = MAX_TASK_FILE_BYTES


class InvalidDedupeMetadata(ValueError):
    """Task dedupe metadata is malformed and should retain a precise failure reason."""


def _serialized_task_bytes(task: dict[str, Any]) -> bytes:
    return json.dumps(
        task,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def task_digest(task: dict[str, Any]) -> str:
    return hashlib.sha256(_serialized_task_bytes(task)).hexdigest()


def task_agent_binding(
    task: dict[str, Any],
    *,
    required: bool = False,
) -> str | None:
    raw = task.get("agent_binding")
    if raw is None:
        if required:
            raise ValueError("agent_binding is required")
        return None
    return canonical_agent_binding(raw)


def require_task_agent_binding(task: dict[str, Any], expected_agent_binding: str) -> str:
    expected = canonical_agent_binding(expected_agent_binding, field="expected_agent_binding")
    provided = task_agent_binding(task, required=True)
    assert provided is not None
    if not secrets.compare_digest(provided, expected):
        raise ValueError(
            f"agent_binding mismatch: expected {expected}, got {provided}"
        )
    return provided


def task_resources_for(task: dict[str, Any]) -> tuple[str, ...]:
    if "resources" not in task:
        raise ValueError("resources must be declared explicitly")
    raw = task["resources"]
    if not isinstance(raw, list):
        raise ValueError("resources must be a list")
    if len(raw) > MAX_TASK_RESOURCES:
        raise ValueError(f"resources exceeds {MAX_TASK_RESOURCES} items")

    resources: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("resources items must be strings")
        if not item or item != item.strip() or item != item.casefold():
            raise ValueError(f"resource must be canonical lowercase text: {item!r}")
        if not _RESOURCE_RE.fullmatch(item):
            raise ValueError(f"invalid resource name: {item!r}")
        if item in seen:
            raise ValueError(f"duplicate resource: {item!r}")
        seen.add(item)
        resources.append(item)

    if "machine" in seen and len(resources) != 1:
        raise ValueError("resource 'machine' must be declared alone")
    return tuple(resources)


def task_dedupe_identity(task: dict[str, Any]) -> tuple[str | None, int]:
    key = task.get("dedupe_key")
    if key is None:
        if "dedupe_revision" in task:
            raise InvalidDedupeMetadata("dedupe_revision requires an explicit dedupe_key")
        return None, 1
    if not isinstance(key, str):
        raise InvalidDedupeMetadata("dedupe_key must be a string")
    if not key or key != key.strip() or len(key) > MAX_DEDUPE_KEY_CHARS:
        raise InvalidDedupeMetadata(
            "dedupe_key must be canonical non-empty text up to 200 characters"
        )
    if not _DEDUPE_KEY_RE.fullmatch(key):
        raise InvalidDedupeMetadata("dedupe_key contains unsupported characters")
    revision = task.get("dedupe_revision", 1)
    if type(revision) is not int or not 1 <= revision <= 1_000_000:
        raise InvalidDedupeMetadata(
            "dedupe_revision must be an integer between 1 and 1000000"
        )
    return key, revision


def _reject_local_codex(command: str, *, field: str) -> None:
    if _invokes_local_codex(command):
        raise ValueError(
            f"{field} may not invoke local Codex; ChatGPT is the planner and "
            "Local Agent executes deterministic commands only"
        )


def _invokes_local_codex(command: str, *, depth: int = 0) -> bool:
    """Recognize command positions, not mentions in filenames or search arguments.

    This planner policy is not a shell sandbox or an executable allowlist.
    """
    if depth > 8:
        raise ValueError("nested shell commands exceed policy inspection depth")
    if not re.search(r"\bcodex\b", command, re.IGNORECASE):
        return False
    command, substitutions = _shell_policy_sources(command)
    for nested in _shell_substitutions(substitutions):
        if _invokes_local_codex(nested, depth=depth + 1):
            return True
    lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|()<>\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise ValueError(f"invalid shell command: {exc}") from exc
    segments: list[list[str]] = [[]]
    comment = False
    for token in tokens:
        if token.startswith("#"):
            comment = True
        if token and all(char in ";&|()\n" for char in token):
            segments.append([])
            if "\n" in token:
                comment = False
        elif comment:
            continue
        else:
            if token.startswith(("'", '"')):
                token = shlex.split(token)[0]
            segments[-1].append(token)
    wrappers = {"env", "sudo", "command", "exec", "timeout", "nice", "nohup", "time"}
    option_values = {"-u", "-g", "-C", "--user", "--group", "--chdir", "--unset", "-n"}
    for words in segments:
        index = 0
        while index < len(words):
            word = words[index]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word) or word in {
                "if", "then", "do", "elif", "!", "{"
            }:
                index += 1
                continue
            executable = PurePosixPath(word).name.casefold()
            if executable in {"codex", "eval"}:
                return True
            if executable in {"npx", "pnpm", "yarn", "bun"}:
                args = words[index + 1:]
                while args and (args[0].startswith("-") or args[0] in {"exec", "dlx", "x"}):
                    args = args[1:]
                if args and re.fullmatch(r"(?:@openai/)?codex(?:@[^\s]+)?", args[0], re.IGNORECASE):
                    return True
            if executable in {"sh", "bash", "zsh"}:
                for offset, argument in enumerate(words[index + 1:], index + 1):
                    if argument.startswith("-") and "c" in argument[1:] and offset + 1 < len(words):
                        if _invokes_local_codex(words[offset + 1], depth=depth + 1):
                            return True
                        break
            if executable not in wrappers:
                break
            index += 1
            while index < len(words) and words[index].startswith("-"):
                option = words[index]
                index += 2 if option in option_values else 1
            if executable == "timeout" and index < len(words):
                index += 1
    return False


def _shell_policy_sources(command: str) -> tuple[str, str]:
    """Exclude heredoc data from command positions; expand only unquoted bodies."""
    source: list[str] = []
    substitutions: list[str] = []
    pending: list[tuple[str, bool, bool]] = []
    for line in command.splitlines(keepends=True):
        if pending:
            delimiter, expand, strip_tabs = pending[0]
            text = line.lstrip("\t") if strip_tabs else line
            if text.rstrip("\r\n") == delimiter:
                pending.pop(0)
            elif expand:
                substitutions.append(line)
            continue
        source.append(line)
        substitutions.append(line)
        if "<<" not in line:
            continue
        lexer = shlex.shlex(line, posix=False, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
        for index, token in enumerate(tokens[:-1]):
            if token == "<<":
                raw = tokens[index + 1]
                strip_tabs = raw.startswith("-")
                raw = raw.removeprefix("-")
                quoted = raw.startswith(("'", '"'))
                delimiter = shlex.split(raw)[0] if quoted else raw
                pending.append((delimiter, not quoted, strip_tabs))
    return "".join(source), "".join(substitutions)


def _shell_substitutions(source: str) -> list[str]:
    """Extract recognizable command substitutions outside single-quoted data."""
    result: list[str] = []
    quote = ""
    index = 0
    while index < len(source):
        char = source[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if char in {"'", '"'}:
            if char == quote:
                quote = ""
            elif not quote:
                quote = char
        if quote != "'" and source.startswith("$(", index):
            start = index + 2
            end = start
            nesting = 1
            inner_quote = ""
            while end < len(source) and nesting:
                current = source[end]
                if current == "\\" and inner_quote != "'":
                    end += 2
                    continue
                if current in {"'", '"'}:
                    if current == inner_quote:
                        inner_quote = ""
                    elif not inner_quote:
                        inner_quote = current
                if not inner_quote:
                    nesting += (current == "(") - (current == ")")
                end += 1
            if nesting == 0:
                result.append(source[start:end - 1])
                index = end
                continue
            break
        elif quote != "'" and char == "`":
            end = source.find("`", index + 1)
            if end >= 0:
                result.append(source[index + 1:end])
                index = end + 1
                continue
        index += 1
    return result


def _payload_reference(value: Any, *, field: str) -> str | None:
    if not isinstance(value, dict) or TASK_PAYLOAD_REF_KEY not in value:
        return None
    if set(value) != {TASK_PAYLOAD_REF_KEY}:
        raise ValueError(
            f"{field} payload reference must contain only {TASK_PAYLOAD_REF_KEY!r}"
        )
    raw = value[TASK_PAYLOAD_REF_KEY]
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{field} payload file must be a non-empty string")
    return raw


def _read_payload_text(
    task_id: str,
    relative_path: str,
    *,
    field: str,
    max_bytes: int,
    max_chars: int | None,
    state: dict[str, Any],
) -> str:
    pure = PurePosixPath(relative_path)
    if (
        pure.is_absolute()
        or pure.as_posix() != relative_path
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError(f"{field} payload path must be canonical relative POSIX text")

    payload_root_name = f"{task_id}.payload"
    if len(pure.parts) < 2 or pure.parts[0] != payload_root_name:
        raise ValueError(
            f"{field} payload path must stay under {payload_root_name}/"
        )

    cache = state["cache"]
    cached = cache.get(relative_path)
    if cached is not None:
        text = str(cached)
    else:
        from linux_agent.foundation import core as core_module

        tasks_root = (state["tasks_root"] or core_module.CONTROL / ".agent/tasks").resolve()
        current = tasks_root
        for part in pure.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"{field} payload path may not contain symlinks")

        payload_root = (tasks_root / payload_root_name).resolve()
        target = current.resolve()
        if payload_root not in target.parents:
            raise ValueError(f"{field} payload path escapes its task payload directory")
        if not target.is_file():
            raise ValueError(f"{field} payload file does not exist: {relative_path}")

        size = target.stat().st_size
        if size > max_bytes:
            raise ValueError(f"{field} payload exceeds {max_bytes} bytes")
        if relative_path not in state["seen"]:
            state["seen"].add(relative_path)
            state["bytes"] += size
            if len(state["seen"]) > MAX_TASK_PAYLOAD_FILES:
                raise ValueError(
                    f"task payload exceeds {MAX_TASK_PAYLOAD_FILES} files"
                )
            if state["bytes"] > MAX_TASK_PAYLOAD_BYTES:
                raise ValueError(
                    f"task payload exceeds {MAX_TASK_PAYLOAD_BYTES} total bytes"
                )
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"{field} payload must be valid UTF-8: {relative_path}"
            ) from exc
        cache[relative_path] = text

    encoded_bytes = len(text.encode("utf-8"))
    if encoded_bytes > max_bytes:
        raise ValueError(f"{field} payload exceeds {max_bytes} bytes")
    if max_chars is not None and len(text) > max_chars:
        raise ValueError(f"{field} payload exceeds {max_chars} characters")
    return text


def _resolve_payload_text(
    task_id: str,
    value: Any,
    *,
    field: str,
    max_bytes: int,
    max_chars: int | None,
    state: dict[str, Any],
) -> Any:
    relative_path = _payload_reference(value, field=field)
    if relative_path is None:
        return value
    return _read_payload_text(
        task_id,
        relative_path,
        field=field,
        max_bytes=max_bytes,
        max_chars=max_chars,
        state=state,
    )


def _materialize_task_payloads(task: dict[str, Any], task_id: str, tasks_root: Path | None) -> None:
    state: dict[str, Any] = {"cache": {}, "seen": set(), "bytes": 0, "tasks_root": tasks_root}

    if "patch" in task:
        task["patch"] = _resolve_payload_text(
            task_id,
            task["patch"],
            field="patch",
            max_bytes=MAX_PATCH_BYTES,
            max_chars=None,
            state=state,
        )

    writes = task.get("writes")
    if isinstance(writes, list):
        for index, item in enumerate(writes):
            if isinstance(item, dict) and "content" in item:
                item["content"] = _resolve_payload_text(
                    task_id,
                    item["content"],
                    field=f"writes[{index}].content",
                    max_bytes=MAX_WRITE_BYTES,
                    max_chars=None,
                    state=state,
                )

    command_bytes = MAX_COMMAND_CHARS * 4
    for field in ("commands", "verify_commands"):
        commands = task.get(field)
        if not isinstance(commands, list):
            continue
        for index, command in enumerate(commands):
            commands[index] = _resolve_payload_text(
                task_id,
                command,
                field=f"{field}[{index}]",
                max_bytes=command_bytes,
                max_chars=MAX_COMMAND_CHARS,
                state=state,
            )

    for field in ("steps", "verify_steps"):
        steps = task.get(field)
        if not isinstance(steps, list):
            continue
        for index, item in enumerate(steps):
            if isinstance(item, dict) and "command" in item:
                item["command"] = _resolve_payload_text(
                    task_id,
                    item["command"],
                    field=f"{field}[{index}].command",
                    max_bytes=command_bytes,
                    max_chars=MAX_COMMAND_CHARS,
                    state=state,
                )


def validate_task(
    task: dict[str, Any], *, require_agent_binding: bool = False,
    tasks_root: Path | None = None, allow_local_codex: bool = False,
) -> None:
    if not isinstance(task, dict):
        raise ValueError("task must be an object")
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id or len(task_id) > 200:
        raise ValueError("task id must be a non-empty string up to 200 characters")
    if not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError("task id contains unsupported characters")
    task_agent_binding(task, required=require_agent_binding)
    mode = task.get("mode", "commands")
    if not isinstance(mode, str) or mode != "commands":
        raise ValueError("only mode=commands is supported")
    if "allow_write" in task and not isinstance(task["allow_write"], bool):
        raise ValueError("allow_write must be a boolean")
    if "work_branch" in task and not isinstance(task["work_branch"], str):
        raise ValueError("work_branch must be a string")
    task_resources_for(task)
    task_dedupe_identity(task)
    for field in (
        "writes",
        "deletes",
        "commands",
        "verify_commands",
        "steps",
        "verify_steps",
    ):
        if field in task and not isinstance(task[field], list):
            raise ValueError(f"{field} must be a list")
        if len(task.get(field, [])) > MAX_TASK_LIST_ITEMS:
            raise ValueError(f"{field} exceeds {MAX_TASK_LIST_ITEMS} items")

    _materialize_task_payloads(task, task_id, tasks_root)

    resolved_size = len(_serialized_task_bytes(task))
    if resolved_size > MAX_TASK_FILE_BYTES:
        raise ValueError(
            f"resolved task exceeds {MAX_TASK_FILE_BYTES} bytes: {resolved_size}"
        )

    patch = task.get("patch")
    if patch is not None:
        if not isinstance(patch, str):
            raise ValueError("patch must be a string")
        if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
            raise ValueError(f"patch exceeds {MAX_PATCH_BYTES} bytes")

    total_write_bytes = 0
    for item in task.get("writes", []):
        if not isinstance(item, dict):
            raise ValueError("writes items must be objects")
        path = item.get("path")
        content = item.get("content")
        if not isinstance(path, str) or not path or len(path) > MAX_TASK_PATH_CHARS:
            raise ValueError("write path must be a non-empty bounded string")
        if not isinstance(content, str):
            raise ValueError(f"write content must be a string for {path!r}")
        write_bytes = len(content.encode("utf-8"))
        if write_bytes > MAX_WRITE_BYTES:
            raise ValueError(f"write content for {path!r} exceeds {MAX_WRITE_BYTES} bytes")
        total_write_bytes += write_bytes
    if total_write_bytes > MAX_TOTAL_WRITE_BYTES:
        raise ValueError(f"writes exceed {MAX_TOTAL_WRITE_BYTES} total bytes")

    for path in task.get("deletes", []):
        if not isinstance(path, str) or not path or len(path) > MAX_TASK_PATH_CHARS:
            raise ValueError("delete paths must be non-empty bounded strings")

    for field in ("commands", "verify_commands"):
        for command in task.get(field, []):
            if not isinstance(command, str) or not command.strip():
                raise ValueError(f"{field} items must be non-empty strings")
            if len(command) > MAX_COMMAND_CHARS:
                raise ValueError(f"{field} item exceeds {MAX_COMMAND_CHARS} characters")
            if not allow_local_codex:
                _reject_local_codex(command, field=field)

    for field in ("steps", "verify_steps"):
        for item in task.get(field, []):
            if not isinstance(item, dict):
                raise ValueError(f"{field} items must be objects")
            command = item.get("command")
            if isinstance(command, str):
                if len(command) > MAX_COMMAND_CHARS:
                    raise ValueError(f"{field} item command exceeds {MAX_COMMAND_CHARS} characters")
                if not allow_local_codex:
                    _reject_local_codex(command, field=field)
    from linux_agent.foundation import core as core_module

    stage_plan = core_module.stage_plan_for(task)
    command_timeout = core_module.command_timeout_for(task)
    idle_timeout_for(task)
    task_timeout = task_timeout_for(task)
    memory_limit_for(task)
    for stage in stage_plan:
        stage_timeout = int(stage.get("stage_timeout", command_timeout))
        if stage_timeout + TASK_FINALIZATION_RESERVE > task_timeout:
            raise ValueError(
                f"stage {stage['stage_name']!r} timeout {stage_timeout}s cannot fit "
                f"inside task_timeout={task_timeout}s with "
                f"{TASK_FINALIZATION_RESERVE}s finalization reserve"
            )


def _bounded_int(
    task: dict[str, Any],
    field: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = task.get(field, default)
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ValueError(f"invalid {field}: {raw!r}") from None
    value = raw
    if value < minimum or value > maximum:
        raise ValueError(f"{field} must be {minimum}..{maximum}, got {value}")
    return value


def idle_timeout_for(task: dict[str, Any]) -> int:
    return _bounded_int(task, "idle_timeout", DEFAULT_IDLE_TIMEOUT, 0, MAX_IDLE_TIMEOUT)


def task_timeout_for(task: dict[str, Any]) -> int:
    return _bounded_int(task, "task_timeout", DEFAULT_TASK_TIMEOUT, 1, MAX_TASK_TIMEOUT)


def memory_limit_for(task: dict[str, Any]) -> int:
    return _bounded_int(
        task,
        "memory_limit_mb",
        DEFAULT_MEMORY_LIMIT_MB,
        MIN_MEMORY_LIMIT_MB,
        MAX_MEMORY_LIMIT_MB,
    )
