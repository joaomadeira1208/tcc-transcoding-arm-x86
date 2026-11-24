"""O arquivo de estado da campanha — funções puras, sem I/O."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Any

from experiment_config import CpuOptions
from field_checks import FieldError, check_fields, check_int, check_non_empty_str
from status_check import (
    DoneMarker,
    JudgeProgress,
    Progress,
    Role,
    StatusError,
    StatusKeys,
    check_done_marker,
    check_judge_progress,
    check_progress,
    judge_progress_line,
    progress_line,
)
from vigilance import Vigilance


class StateError(Exception):
    """Arquivo de estado que o Orquestrador recusa a ler, com o campo nomeado."""


@dataclass(frozen=True)
class TrackedInstance:
    role: Role
    instance: str
    instance_id: str
    instance_type: str
    pid: int | None
    block_count: int
    runs_total: int
    commit: str
    total_timeout: int
    state: Vigilance
    outcome: DoneMarker | None
    cpu_options: CpuOptions | None

    @property
    def status_keys(self) -> StatusKeys:
        return StatusKeys.of(self.role, self.instance)

    def read_progress(self, payload: Any) -> Progress | JudgeProgress | None:
        if self.role is Role.JUDGE:
            return check_judge_progress(payload, instance_id=self.instance_id)
        return check_progress(payload, instance_id=self.instance_id)

    def render_progress(self, progress: Progress | JudgeProgress | None) -> str:
        if self.role is Role.JUDGE:
            return judge_progress_line(progress)
        return progress_line(progress, instance=self.instance, runs_total=self.runs_total)


@dataclass(frozen=True)
class CampaignState:
    bucket: str
    config_path: str
    commit: str
    total_timeout: int
    slice_keys: tuple[str, ...]
    instances: tuple[TrackedInstance, ...]


def parse_state(payload: Any) -> CampaignState:
    values = _values(CampaignState, _object(payload, "estado"), "")
    launch = {"commit": values["commit"], "total_timeout": values["total_timeout"]}
    return CampaignState(
        **{
            **values,
            "slice_keys": _slice_keys(values["slice_keys"]),
            "instances": tuple(
                _tracked(entry, f"instances[{index}]", launch)
                for index, entry in enumerate(values["instances"])
            ),
        }
    )


def appended(state: CampaignState, entry: TrackedInstance) -> CampaignState:
    return replace(state, instances=(*state.instances, entry))


def serialize_state(state: CampaignState) -> str:
    return json.dumps(asdict(state), indent=2, separators=(",", ": "), default=_enum_value) + "\n"


def _tracked(payload: Any, where: str, launch: Mapping[str, Any]) -> TrackedInstance:
    raw = {"role": Role.ENCODE.value, "cpu_options": None, **launch, **_object(payload, where)}
    values = _values(TrackedInstance, raw, f"{where}.")
    return TrackedInstance(
        **{
            **values,
            "role": Role(values["role"]),
            "state": Vigilance(values["state"]),
            "outcome": _outcome(values["outcome"], values["instance_id"], f"{where}.outcome"),
            "cpu_options": _cpu_options(values["cpu_options"], f"{where}.cpu_options"),
        }
    )


def _cpu_options(payload: Any, where: str) -> CpuOptions | None:
    if payload is None:
        return None
    return CpuOptions(**_values(CpuOptions, payload, f"{where}."))


def _outcome(payload: Any, instance_id: str, where: str) -> DoneMarker | None:
    """O marcador é conferido pelo leitor que é dono dele, e pela identidade também: um
    estado que guardasse o marcador da tentativa anterior daria a fatia por completa sem
    que uma Execução desta tivesse rodado, e o `resume.py` não seria chamado para
    nenhuma delas.
    """
    if payload is None:
        return None
    try:
        marker = check_done_marker(payload, instance_id=instance_id)
    except StatusError as error:
        raise StateError(f"{where}: {error}") from error
    if marker is None:
        raise StateError(f"{where}: o marcador guardado é de outra instância, não de {instance_id}")
    return marker


def _slice_keys(listed: list[Any]) -> tuple[str, ...]:
    try:
        for index, key in enumerate(listed):
            check_non_empty_str(f"slice_keys[{index}]", key)
    except FieldError as error:
        raise StateError(str(error)) from error
    return tuple(listed)


def _values(record: type, raw: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    try:
        return check_fields(record, raw, _CHECKS)
    except FieldError as error:
        raise StateError(f"{prefix}{error}") from error


def _object(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StateError(f"{where}: esperava um objeto, veio {type(value).__name__}")
    return value


def _check_list(field: str, value: Any) -> None:
    if not isinstance(value, list):
        raise FieldError(f"{field}: esperava uma lista, veio {type(value).__name__}")


def _check_pid(field: str, value: Any) -> None:
    if value is None:
        return
    check_int(field, value)
    if value <= 0:
        raise FieldError(f"{field}: esperava PID positivo, veio {value!r}")


def _check_outcome(field: str, value: Any) -> None:
    if value is not None and not isinstance(value, Mapping):
        raise FieldError(f"{field}: esperava o marcador ou nulo, veio {type(value).__name__}")


def _check_cpu_options(field: str, value: Any) -> None:
    if value is not None and not isinstance(value, Mapping):
        raise FieldError(f"{field}: esperava a topologia ou nulo, veio {type(value).__name__}")


def _check_timeout(field: str, value: Any) -> None:
    check_int(field, value)
    if value <= 0:
        raise FieldError(f"{field}: esperava segundos positivos, veio {value!r}")


def _check_state(field: str, value: Any) -> None:
    try:
        Vigilance(value)
    except ValueError as error:
        raise FieldError(f"{field}: estado desconhecido: {value!r}") from error


def _check_role(field: str, value: Any) -> None:
    try:
        Role(value)
    except ValueError as error:
        raise FieldError(f"{field}: papel desconhecido: {value!r}") from error


def _enum_value(value: Any) -> str:
    if not isinstance(value, Enum):
        raise TypeError(f"{type(value).__name__} não é serializável no arquivo de estado")
    return value.value


_CHECKS: dict[str, Callable[[str, Any], None]] = {
    "bucket": check_non_empty_str,
    "config_path": check_non_empty_str,
    "commit": check_non_empty_str,
    "total_timeout": _check_timeout,
    "slice_keys": _check_list,
    "instances": _check_list,
    "role": _check_role,
    "instance": check_non_empty_str,
    "instance_id": check_non_empty_str,
    "instance_type": check_non_empty_str,
    "pid": _check_pid,
    "block_count": check_int,
    "runs_total": check_int,
    "state": _check_state,
    "outcome": _check_outcome,
    "cpu_options": _check_cpu_options,
    "core_count": check_int,
    "threads_per_core": check_int,
}
