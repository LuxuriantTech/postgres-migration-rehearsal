"""Control-room-only synchronization primitives."""

from migration_rehearsal.control_room_runtime.sync import wait_for_process, wait_for_retry

__all__ = ["wait_for_process", "wait_for_retry"]
