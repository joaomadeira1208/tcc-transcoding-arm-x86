"""Construção do plano de Cenários — funções puras, sem I/O."""

from __future__ import annotations

import json
import random
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from experiment_config import (
    CodecRecord,
    ExperimentConfig,
    InstanceRecord,
    MetricRecord,
    PairRecord,
    VideoRecord,
)
from masters_plan import master_name

SCHEMA_VERSION = "1"

WARMUP_SUFFIX = "warmup"


@dataclass(frozen=True)
class Scenario:
    codec: CodecRecord
    pair: PairRecord
    video: VideoRecord
    instance: InstanceRecord


def build_canonical_plan(config: ExperimentConfig) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "seed": config.seed,
        "blocks": [_block(config, scenario) for scenario in _scenarios(config)],
    }


def build_instance_slices(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {instance: _slice(plan, instance) for instance in _instances(plan)}


def serialize_plan(plan: dict[str, Any]) -> str:
    """Sem `generated_at` nem qualquer campo de ambiente: é o que faz "reproduzir o
    plano" ser verificável com `diff`.
    """
    return json.dumps(plan, indent=2, separators=(",", ": ")) + "\n"


def summarize_plan(plan: dict[str, Any]) -> str:
    runs = [run for block in plan["blocks"] for run in block["runs"]]
    replications = sum(1 for run in runs if not run["warmup"])
    return f"{len(plan['blocks'])} blocks, {len(runs)} runs, {replications} replications"


def _slice(plan: dict[str, Any], instance: str) -> dict[str, Any]:
    return {**plan, "blocks": [block for block in plan["blocks"] if block["instance"] == instance]}


def _instances(plan: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(block["instance"] for block in plan["blocks"]))


def _scenarios(config: ExperimentConfig) -> Iterator[Scenario]:
    order = _shuffled_combinations(config)
    for instance in config.instances:
        for codec, pair, video in order:
            yield Scenario(codec=codec, pair=pair, video=video, instance=instance)


def _shuffled_combinations(
    config: ExperimentConfig,
) -> list[tuple[CodecRecord, PairRecord, VideoRecord]]:
    """Gerador próprio, nunca o estado global do módulo `random`: com o global,
    qualquer outra chamada no processo entraria no plano e "mesmo TOML ⇒ mesmos
    bytes" deixaria de valer sem que nada falhasse.
    """
    combinations = list(_combinations(config))
    random.Random(config.seed).shuffle(combinations)
    return combinations


def _combinations(
    config: ExperimentConfig,
) -> Iterator[tuple[CodecRecord, PairRecord, VideoRecord]]:
    """Laços sobre as listas, sem conjunto nem dicionário no caminho: a ordem
    pré-shuffle precisa ser determinística para que a pós-shuffle seja.
    """
    for codec in config.codecs:
        for pair in config.pairs:
            for video in config.videos:
                yield codec, pair, video


def _block(config: ExperimentConfig, scenario: Scenario) -> dict[str, Any]:
    return {
        **_identity(scenario),
        "runs": [
            _run(config, scenario, suffix)
            for suffix in _suffixes(config.warmup_runs, config.replications)
        ],
    }


def _run(config: ExperimentConfig, scenario: Scenario, suffix: str) -> dict[str, Any]:
    codec = scenario.codec
    encode = config.encode
    output = scenario.video.geometry[scenario.pair.output_res]
    return {
        "scenario_id": _scenario_id(scenario, suffix),
        "warmup": suffix == WARMUP_SUFFIX,
        "seed": config.seed,
        **_identity(scenario),
        "master": master_name(scenario.video.slug, scenario.pair.input_res),
        "output_width": output.width,
        "output_height": output.height,
        "preset": codec.preset,
        "crf": codec.crf,
        "encoder_args": list(codec.encoder_args),
        "threads": encode.threads,
        "gop_size": encode.gop_size,
        "pix_fmt": encode.pix_fmt,
        "strip_audio": encode.strip_audio,
        "container": encode.container,
        "scale_flags": encode.scale_flags,
        "bitstream_muxer": codec.bitstream_muxer,
        "pmu_events": list(config.instrumentation.pmu_events),
        "perf_event_spec": config.instrumentation.event_spec,
        "pmu_hardware_events": list(config.instrumentation.hardware_events),
        "pmu_metrics": [_metric(metric) for metric in config.instrumentation.metrics],
    }


def _metric(metric: MetricRecord) -> dict[str, Any]:
    return {
        "name": metric.name,
        "numerator": metric.numerator,
        "denominator": metric.denominator,
        "max_ratio": metric.max_ratio,
    }


def _identity(scenario: Scenario) -> dict[str, Any]:
    """Repetir o dado é deliberado: o run precisa bastar sozinho para o bash, que só
    recebe o objeto de run.
    """
    return {
        "codec": scenario.codec.codec,
        "encoder": scenario.codec.encoder,
        "input_res": scenario.pair.input_res,
        "output_res": scenario.pair.output_res,
        "video": scenario.video.slug,
        "instance": scenario.instance.id,
    }


def _scenario_id(scenario: Scenario, suffix: str) -> str:
    return "_".join(
        (
            scenario.codec.slug,
            scenario.pair.input_res,
            scenario.pair.output_res,
            scenario.video.slug,
            scenario.instance.id,
            suffix,
        )
    )


def _suffixes(warmup_runs: int, replications: int) -> list[str]:
    return [WARMUP_SUFFIX] * warmup_runs + [f"rep{n}" for n in range(1, replications + 1)]
