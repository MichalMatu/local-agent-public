from __future__ import annotations

import hashlib
import re

SUMMARY_FAILURE_TAIL_CHARS = 8000
LIVE_DIFF_MAX_LINES = 80
LIVE_DIFF_MAX_CHARS = 12_000

_DIFF_METADATA_PREFIXES = (
    "index ",
    "--- ",
    "+++ ",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "Binary files ",
    "GIT binary patch",
    "literal ",
    "delta ",
    "\\ No newline at end of file",
)

_DIFF_HUNK_RE = re.compile(
    r"^@@ -\d+(?:,(?P<old_count>\d+))? "
    r"\+\d+(?:,(?P<new_count>\d+))? @@(?:.*)$"
)


class LiveCommandOutput:
    """Keep normal live output readable while retaining raw command output separately."""

    def __init__(self) -> None:
        self._in_diff = False
        self._in_hunk = False
        self._old_remaining = 0
        self._new_remaining = 0
        self._collapsed = False
        self._buffer: list[str] = []
        self._files = 0
        self._lines = 0
        self._chars = 0
        self._fingerprint = hashlib.sha256()
        self._last_collapsed_fingerprint: str | None = None
        self._repeated_collapsed_count = 0

    @staticmethod
    def _print_line(line: str) -> None:
        print(f"[CMD] {line}", end="", flush=True)

    def _reset_diff(self) -> None:
        self._in_diff = False
        self._in_hunk = False
        self._old_remaining = 0
        self._new_remaining = 0
        self._collapsed = False
        self._buffer.clear()
        self._files = 0
        self._lines = 0
        self._chars = 0
        self._fingerprint = hashlib.sha256()

    @staticmethod
    def _parse_hunk_header(line: str) -> tuple[int, int] | None:
        match = _DIFF_HUNK_RE.match(line.rstrip("\r\n"))
        if match is None:
            return None
        old_count = int(match.group("old_count") or "1")
        new_count = int(match.group("new_count") or "1")
        return old_count, new_count

    @staticmethod
    def _is_diff_metadata_line(line: str) -> bool:
        return line.rstrip("\r\n").startswith(_DIFF_METADATA_PREFIXES)

    def _consume_hunk_line(self, line: str) -> bool:
        if not self._in_hunk:
            return False
        text = line.rstrip("\r\n")
        if text.startswith(" "):
            self._old_remaining -= 1
            self._new_remaining -= 1
        elif text.startswith("-"):
            self._old_remaining -= 1
        elif text.startswith("+"):
            self._new_remaining -= 1
        else:
            return False
        self._record_diff_line(line)
        if self._old_remaining <= 0 and self._new_remaining <= 0:
            self._in_hunk = False
        return True

    def _record_diff_line(self, line: str) -> None:
        if line.startswith("diff --git "):
            self._files += 1
        self._lines += 1
        self._chars += len(line)
        self._fingerprint.update(line.encode("utf-8"))
        if not self._collapsed:
            self._buffer.append(line)
            if self._lines > LIVE_DIFF_MAX_LINES or self._chars > LIVE_DIFF_MAX_CHARS:
                self._collapsed = True
                self._buffer.clear()

    def _flush_repeated_notice(self) -> None:
        if self._repeated_collapsed_count == 0:
            return
        count = self._repeated_collapsed_count
        copies = "copy" if count == 1 else "copies"
        print(
            f"[CMD] [suppressed {count} repeated {copies} of the previous unified diff]",
            flush=True,
        )
        self._last_collapsed_fingerprint = None
        self._repeated_collapsed_count = 0

    def _emit_collapsed_diff(self) -> None:
        fingerprint = self._fingerprint.hexdigest()
        if fingerprint == self._last_collapsed_fingerprint:
            self._repeated_collapsed_count += 1
            return
        self._flush_repeated_notice()
        print(
            "[CMD] [large unified diff collapsed in live log; "
            "raw output remains in bounded task result buffer]",
            flush=True,
        )
        kib = self._chars / 1024.0
        print(
            f"[CMD] [collapsed unified diff: {self._files} file(s), "
            f"{self._lines} line(s), {kib:.1f} KiB]",
            flush=True,
        )
        self._last_collapsed_fingerprint = fingerprint

    def _flush_diff(self) -> None:
        if not self._in_diff:
            return
        if self._collapsed:
            self._emit_collapsed_diff()
        else:
            self._flush_repeated_notice()
            for line in self._buffer:
                self._print_line(line)
            self._last_collapsed_fingerprint = None
        self._reset_diff()

    def _emit_normal_line(self, line: str) -> None:
        if line.rstrip("\r\n"):
            self._flush_repeated_notice()
            self._last_collapsed_fingerprint = None
        self._print_line(line)

    def emit(self, line: str) -> None:
        if not self._in_diff:
            if line.startswith("diff --git "):
                self._in_diff = True
                self._record_diff_line(line)
                return
            self._emit_normal_line(line)
            return

        if line.startswith("diff --git "):
            self._in_hunk = False
            self._record_diff_line(line)
            return

        hunk_counts = self._parse_hunk_header(line)
        if hunk_counts is not None:
            self._old_remaining, self._new_remaining = hunk_counts
            self._in_hunk = self._old_remaining > 0 or self._new_remaining > 0
            self._record_diff_line(line)
            return

        if self._consume_hunk_line(line):
            return

        if self._is_diff_metadata_line(line):
            self._record_diff_line(line)
            return

        self._flush_diff()
        self._emit_normal_line(line)

    def finish(self) -> None:
        self._flush_diff()
        self._flush_repeated_notice()


def emit_summary_failure_tail(text: str, *, truncated: bool) -> None:
    print("[CMD] [summary stage failed; bounded output tail follows]", flush=True)
    if truncated:
        print(
            f"[CMD] [... truncated; showing last {SUMMARY_FAILURE_TAIL_CHARS} chars ...]",
            flush=True,
        )
    for line in text.splitlines():
        print(f"[CMD] {line}", flush=True)
