"""Named synchronization for bounded control-room lifecycle operations."""

from __future__ import annotations

from threading import Event
from typing import Protocol

_RETRY_GATE = Event()


class WaitableProcess(Protocol):
    """Small structural boundary shared by text and binary subprocesses."""

    def wait(self, timeout: float | None = None) -> int: ...


def wait_for_process(process: WaitableProcess, *, timeout: float) -> int:
    """Reap a known child within the caller's fixed timeout."""
    return process.wait(timeout=timeout)


def wait_for_retry(*, timeout: float) -> None:
    """Wait for a bounded lifecycle retry through the named synchronization gate."""
    _RETRY_GATE.wait(timeout=timeout)
