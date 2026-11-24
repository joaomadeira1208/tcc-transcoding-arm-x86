"""O que o laço de vigilância decide sem perguntar nada."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from campaign_state import CampaignState, TrackedInstance
from status_check import HOUR_WIDTH, INSTANCE_WIDTH, JudgeProgress, Progress, Role, hour_of
from vigilance import UNANSWERED_POLL_LIMIT, Vigilance

POLL_INTERVAL_SECONDS = 300.0
LIVENESS_TIMEOUT_SECONDS = 30.0
DEADLINE_MARGIN_SECONDS = 3600.0

RESUME_OUT_DIR = "~/work/resume"

ORCHESTRATOR_CLI = "python orchestrator/orchestrator.py"
RESUME_CLI = "python orchestrator/resume.py"


def watched(state: CampaignState) -> tuple[TrackedInstance, ...]:
    if state.instances and state.instances[-1].role is Role.JUDGE:
        return state.instances[-1:]
    return tuple(each for each in state.instances if each.role is Role.ENCODE)


def watch_deadline_seconds(*, total_timeout: int, bootstrap_timeout: float) -> float:
    return total_timeout + bootstrap_timeout + DEADLINE_MARGIN_SECONDS


def liveness_command(pid: int) -> list[str]:
    return ["sudo", "kill", "-0", str(pid)]


def poll_line(
    each: TrackedInstance,
    *,
    state: Vigilance,
    progress: Progress | JudgeProgress | None,
    unanswered_polls: int = 0,
) -> str:
    line = each.render_progress(progress)
    if state is Vigilance.RUNNING:
        return line
    return f"{line}  [{_note(state, unanswered_polls)}]"


def settled_line(each: TrackedInstance) -> str:
    return f"{'':{HOUR_WIDTH}} {each.instance:<{INSTANCE_WIDTH}} {_summary(each)}"


def summary_lines(instances: Sequence[TrackedInstance]) -> tuple[str, ...]:
    return tuple(settled_line(each) for each in instances)


def failure_reasons(
    instances: Sequence[TrackedInstance], *, deadline_blown: bool = False
) -> tuple[str, ...]:
    blown = ("o prazo do Orquestrador estourou: o que restava de pé foi terminado",)
    return (blown if deadline_blown else ()) + tuple(
        reason for each in instances for reason in _reasons(each)
    )


def resume_hint(state: CampaignState) -> tuple[str, ...]:
    return (
        f"o próximo passo é o resume.py: {RESUME_CLI} --config {state.config_path} "
        f"--bucket {state.bucket} --out {RESUME_OUT_DIR}",
        f"e depois, sobre o que ele escrever: {ORCHESTRATOR_CLI} --infra <infra.json> "
        f"run --config {state.config_path} --bucket {state.bucket} --slices {RESUME_OUT_DIR}",
    )


def _reasons(each: TrackedInstance) -> Iterator[str]:
    if each.outcome is None:
        yield f"{each.instance}: sem marcador, estado {each.state.value}"
        return
    loop = _LOOPS[each.role]
    if each.outcome.runs_failed:
        yield f"{each.instance}: {each.outcome.runs_failed} run(s) com falha"
    if each.outcome.capped:
        yield f"{each.instance}: o teto do {loop} parou o laço antes do fim"
    if each.outcome.exit_status:
        yield f"{each.instance}: o {loop} saiu com status {each.outcome.exit_status}"


def _summary(each: TrackedInstance) -> str:
    if each.outcome is None:
        return (
            f"{each.state.value}: sem marcador, e o que ela deixou em runs/ é o que o "
            f"resume.py enxerga"
        )
    return (
        f"{each.state.value}: {each.outcome.runs_total}/{each.runs_total} runs, "
        f"{each.outcome.runs_failed} falhas, {_cap(each.outcome.capped)}, "
        f"status {each.outcome.exit_status}, marcador de {hour_of(each.outcome.finished_at)}"
    )


def _cap(capped: bool) -> str:
    return "parada pelo teto" if capped else "sem teto"


def _note(state: Vigilance, unanswered_polls: int) -> str:
    if state is Vigilance.UNRESPONSIVE:
        return (
            f"sem resposta no SSH há {unanswered_polls} poll(s), "
            f"morta depois de {UNANSWERED_POLL_LIMIT}"
        )
    return _NOTES[state]


_LOOPS = {Role.ENCODE: "run_all.sh", Role.JUDGE: "run_quality.sh"}

_NOTES = {
    Vigilance.BOOTSTRAPPING: "bootstrap em curso, ainda sem PID",
    Vigilance.READY_TO_TERMINATE: "marcador válido, terminando agora",
    Vigilance.FINISHED: "terminada",
    Vigilance.DEAD: "morta: sem marcador, e não é relançada",
}
