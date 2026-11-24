"""A decisão de um poll sobre uma arquitetura — função pura, sem I/O."""

from __future__ import annotations

from enum import Enum
from typing import Any

from status_check import DoneMarker, check_done_marker

UNANSWERED_POLL_LIMIT = 3

DESCRIBED_PENDING = "pending"
DESCRIBED_RUNNING = "running"


class Liveness(Enum):
    ALIVE = "alive"
    DEAD = "dead"
    NO_ANSWER = "no answer"
    NOT_DISPATCHED = "not dispatched"


class Marker(Enum):
    VALID = "valid"
    OTHER_INSTANCE = "other instance"
    ABSENT = "absent"


class Vigilance(Enum):
    BOOTSTRAPPING = "bootstrapping"
    RUNNING = "running"
    READY_TO_TERMINATE = "ready to terminate"
    FINISHED = "finished"
    DEAD = "dead"
    UNRESPONSIVE = "unresponsive"


_SETTLED = frozenset({Vigilance.FINISHED, Vigilance.DEAD})


def is_standing(state: Vigilance) -> bool:
    return state not in _SETTLED


def marker_verdict(payload: Any, *, instance_id: str) -> tuple[Marker, DoneMarker | None]:
    if payload is None:
        return Marker.ABSENT, None
    marker = check_done_marker(payload, instance_id=instance_id)
    if marker is None:
        return Marker.OTHER_INSTANCE, None
    return Marker.VALID, marker


def decide_vigilance(
    *,
    described_state: str | None,
    liveness: Liveness,
    marker: Marker,
    unanswered_polls: int,
) -> Vigilance:
    if marker is Marker.VALID:
        return Vigilance.READY_TO_TERMINATE
    if described_state == DESCRIBED_PENDING:
        return Vigilance.BOOTSTRAPPING
    if described_state != DESCRIBED_RUNNING:
        return Vigilance.DEAD
    if liveness is Liveness.NOT_DISPATCHED:
        return Vigilance.BOOTSTRAPPING
    if liveness is Liveness.NO_ANSWER:
        if unanswered_polls > UNANSWERED_POLL_LIMIT:
            return Vigilance.DEAD
        return Vigilance.UNRESPONSIVE
    if liveness is Liveness.DEAD:
        return Vigilance.DEAD
    return Vigilance.RUNNING
