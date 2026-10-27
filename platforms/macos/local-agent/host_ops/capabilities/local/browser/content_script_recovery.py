"""One-shot recovery of stale or missing extension content scripts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from time import monotonic

from ._validation import validate_timeout_seconds
from .inspection import _normalize_loopback_endpoint
from .models import BrowserContentScriptRecoveryResult, BrowserReadinessInspection
from .readiness import (
    BrowserCdpReadinessInspector,
    BrowserReadinessError,
    _normalize_script_fingerprints,
)
from .reload import BrowserCdpReloader, BrowserReloadError, _normalize_expected_url
from .selector_counts import _normalize_selectors
from .snapshot import _validate_target_id

_HEALABLE_DIAGNOSES = frozenset({"content_script_missing", "content_script_stale"})
_NOT_NEEDED_DIAGNOSES = frozenset({"ready", "worker_inactive"})


class BrowserContentScriptRecoveryError(RuntimeError):
    """Raised when bounded content-script recovery cannot be completed safely."""


class BrowserCdpContentScriptRecoverer:
    """Perform at most one guarded reload and verify content-script readiness afterward."""

    def __init__(
        self,
        readiness: BrowserCdpReadinessInspector | None = None,
        reloader: BrowserCdpReloader | None = None,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._readiness = readiness or BrowserCdpReadinessInspector()
        self._reloader = reloader or BrowserCdpReloader()
        self._clock = clock

    def recover(
        self,
        endpoint: str,
        target_id: str,
        expected_url: str,
        selectors: Sequence[str],
        script_fingerprints: Sequence[str],
        *,
        timeout_seconds: float = 30.0,
    ) -> BrowserContentScriptRecoveryResult:
        normalized_endpoint = _normalize_loopback_endpoint(endpoint)
        normalized_target_id = _validate_target_id(target_id)
        normalized_expected_url = _normalize_expected_url(expected_url)
        normalized_selectors = _normalize_selectors(selectors)
        normalized_fingerprints = tuple(
            f"{name}={digest}"
            for name, digest in _normalize_script_fingerprints(script_fingerprints)
        )
        timeout_seconds = validate_timeout_seconds(timeout_seconds, minimum=3.0)

        deadline = self._clock() + timeout_seconds
        before = self._inspect(
            normalized_endpoint,
            normalized_target_id,
            normalized_selectors,
            normalized_fingerprints,
            deadline,
            timeout_seconds,
        )
        if before.url != normalized_expected_url:
            raise BrowserContentScriptRecoveryError(
                "browser content-script recovery expected URL guard did not match current target"
            )

        if before.diagnosis not in _HEALABLE_DIAGNOSES:
            outcome = (
                "not_needed" if before.diagnosis in _NOT_NEEDED_DIAGNOSES else "not_applicable"
            )
            return BrowserContentScriptRecoveryResult(
                endpoint=normalized_endpoint,
                target_id=normalized_target_id,
                expected_url=normalized_expected_url,
                action="none",
                outcome=outcome,
                before=before,
                reload=None,
                after=None,
            )

        try:
            reload_result = self._reloader.reload(
                normalized_endpoint,
                normalized_target_id,
                normalized_expected_url,
                timeout_seconds=self._remaining(deadline, timeout_seconds),
            )
        except BrowserReloadError as exc:
            raise BrowserContentScriptRecoveryError(str(exc)) from exc

        if reload_result.after_url != normalized_expected_url:
            return BrowserContentScriptRecoveryResult(
                endpoint=normalized_endpoint,
                target_id=normalized_target_id,
                expected_url=normalized_expected_url,
                action="reload",
                outcome="target_changed",
                before=before,
                reload=reload_result,
                after=None,
            )

        after = self._inspect(
            normalized_endpoint,
            normalized_target_id,
            normalized_selectors,
            normalized_fingerprints,
            deadline,
            timeout_seconds,
        )
        if after.url != normalized_expected_url:
            raise BrowserContentScriptRecoveryError(
                "browser content-script recovery post-check target URL changed"
            )
        outcome = (
            "recovered"
            if after.dom_ready and after.content_script_state == "ready"
            else "not_recovered"
        )
        return BrowserContentScriptRecoveryResult(
            endpoint=normalized_endpoint,
            target_id=normalized_target_id,
            expected_url=normalized_expected_url,
            action="reload",
            outcome=outcome,
            before=before,
            reload=reload_result,
            after=after,
        )

    def _inspect(
        self,
        endpoint: str,
        target_id: str,
        selectors: tuple[str, ...],
        script_fingerprints: tuple[str, ...],
        deadline: float,
        timeout_seconds: float,
    ) -> BrowserReadinessInspection:
        try:
            return self._readiness.inspect(
                endpoint,
                target_id,
                selectors,
                script_fingerprints,
                timeout_seconds=self._remaining(deadline, timeout_seconds),
            )
        except BrowserReadinessError as exc:
            raise BrowserContentScriptRecoveryError(str(exc)) from exc

    def _remaining(self, deadline: float, timeout_seconds: float) -> float:
        remaining = deadline - self._clock()
        if remaining < 1:
            raise BrowserContentScriptRecoveryError(
                "browser content-script recovery exceeded "
                f"{timeout_seconds:g}s whole-operation timeout"
            )
        return min(remaining, 120.0)
