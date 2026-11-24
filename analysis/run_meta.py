"""Modelo do `meta.json` — o contrato cross-language, do lado que lê."""

from __future__ import annotations

from typing import Literal

from json_contract import NonEmptyStr, Strict, json_schema
from pydantic import AwareDatetime

# Eixo próprio, independente do `schema_version` do plano: os dois valem "1" hoje
# e evoluem por motivos diferentes.
SCHEMA_VERSION = "1"


class RunMeta(Strict):
    """Uma Execução, como o `run_scenario.sh` a registra."""

    schema_version: Literal[SCHEMA_VERSION]

    scenario_id: NonEmptyStr
    warmup: bool
    seed: int
    codec: NonEmptyStr
    encoder: NonEmptyStr
    input_res: NonEmptyStr
    output_res: NonEmptyStr
    video: NonEmptyStr
    instance: NonEmptyStr
    master: NonEmptyStr
    output_width: int
    output_height: int
    preset: NonEmptyStr
    crf: int
    encoder_args: list[NonEmptyStr]
    threads: int
    gop_size: int
    pix_fmt: NonEmptyStr
    strip_audio: bool
    container: NonEmptyStr
    scale_flags: NonEmptyStr

    # `AwareDatetime` porque a dedup "último `started_at` vence" ordena instantes:
    # um naïve perdeu a informação para normalizar, e a leitura é a única janela
    # em que isso é detectável.
    run_id: NonEmptyStr
    started_at: AwareDatetime
    finished_at: AwareDatetime
    exit_code: int

    commit: NonEmptyStr
    instance_id: NonEmptyStr
    instance_type: NonEmptyStr

    versions: dict[NonEmptyStr, NonEmptyStr]


def load_meta(raw: str | bytes) -> RunMeta:
    """Bytes crus, e não `json.load()` antes: em modo estrito o pydantic rejeitaria
    `str` → `datetime` pela porta do Python, e a saída natural dessa dor seria
    voltar para o modo lax pelo motivo errado.
    """
    return RunMeta.model_validate_json(raw)


def render_json_schema() -> str:
    return json_schema(RunMeta)
