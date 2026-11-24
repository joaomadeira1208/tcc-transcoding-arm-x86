from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ABSENT, META_SCHEMA_PATH, make_meta_json
from pydantic import ValidationError
from run_meta import SCHEMA_VERSION, load_meta, render_json_schema


def offending_fields(error: ValidationError) -> set[str]:
    return {".".join(str(part) for part in item["loc"]) for item in error.errors()}


class TestAccepts:
    def test_valid_meta(self):
        meta = load_meta(make_meta_json())

        assert meta.schema_version == SCHEMA_VERSION
        assert meta.scenario_id == "libx264_2160p_1080p_bbb_c7g_rep1"
        assert meta.warmup is False
        assert meta.crf == 23
        assert meta.encoder_args == ["-sc_threshold", "0"]
        assert meta.versions["ffmpeg"] == "n7.1"

    def test_timestamps_arrive_as_strings_and_come_out_aware(self):
        # É JSON: não existe tipo de data, e o bash escreve ISO-8601 com offset.
        meta = load_meta(make_meta_json())

        assert meta.started_at == datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
        assert meta.finished_at.utcoffset() is not None

    def test_offset_other_than_utc_normalizes_to_the_same_instant(self):
        # O caso que justifica exigir offset: `10:00+00:00` e `07:00-03:00` são o
        # mesmo instante e ordenam ao contrário em comparação lexicográfica. A
        # dedup ordena instantes, então o modelo precisa entregar um
        # `datetime` comparável, não a string.
        meta = load_meta(make_meta_json(started_at="2026-08-08T07:00:00-03:00"))

        assert meta.started_at == datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
        assert meta.started_at.utcoffset() == timedelta(hours=-3)


class TestRejects:
    def test_warmup_as_string(self):
        # O modo de falha canônico do arquivo: `"false"` é string truthy, e em
        # modo lax o pydantic a coagiria para `False` calado — fazendo este
        # leitor discordar do checador stdlib sobre o que é um arquivo válido.
        with pytest.raises(ValidationError) as caught:
            load_meta(make_meta_json(warmup="false"))

        assert "warmup" in offending_fields(caught.value)

    def test_crf_as_string(self):
        with pytest.raises(ValidationError) as caught:
            load_meta(make_meta_json(crf="23"))

        assert "crf" in offending_fields(caught.value)

    def test_missing_required_field(self):
        with pytest.raises(ValidationError) as caught:
            load_meta(make_meta_json(warmup=ABSENT))

        assert "warmup" in offending_fields(caught.value)

    def test_unknown_schema_version(self):
        with pytest.raises(ValidationError) as caught:
            load_meta(make_meta_json(schema_version="2"))

        assert "schema_version" in offending_fields(caught.value)

    def test_naive_started_at(self):
        with pytest.raises(ValidationError) as caught:
            load_meta(make_meta_json(started_at="2026-08-08T10:00:00"))

        assert "started_at" in offending_fields(caught.value)

    def test_naive_finished_at(self):
        with pytest.raises(ValidationError) as caught:
            load_meta(make_meta_json(finished_at="2026-08-08T10:12:31"))

        assert "finished_at" in offending_fields(caught.value)

    def test_empty_scenario_id(self):
        # `jq -r` sobre uma chave presente e vazia devolve string vazia, e uma
        # `scenario_id` vazia casaria com nada na consolidação.
        with pytest.raises(ValidationError) as caught:
            load_meta(make_meta_json(scenario_id=""))

        assert "scenario_id" in offending_fields(caught.value)

    def test_unknown_field(self):
        # Campo desconhecido é mudança de forma, e mudança de forma passa pelo
        # `schema_version`.
        with pytest.raises(ValidationError) as caught:
            load_meta(make_meta_json(output_sha256="d41d8cd9"))

        assert "output_sha256" in offending_fields(caught.value)

    def test_malformed_json(self):
        with pytest.raises(ValidationError):
            load_meta("{")


class TestCommittedJsonSchema:
    def test_matches_the_model(self):
        assert META_SCHEMA_PATH.read_text(encoding="utf-8") == render_json_schema(), (
            "meta.schema.json fora de sincronia com o modelo; regenere com "
            "`python analysis/validate_meta.py --emit-schema > analysis/meta.schema.json`"
        )

    def test_describes_the_same_required_fields(self):
        schema = json.loads(META_SCHEMA_PATH.read_text(encoding="utf-8"))

        assert set(schema["required"]) == set(json.loads(make_meta_json()))
