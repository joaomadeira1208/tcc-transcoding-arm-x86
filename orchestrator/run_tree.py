"""A leitura da árvore `runs/` sincronizada: os `meta.json` e os `output.sha256`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from external import RUN_HASH_FILENAME, RUN_META_FILENAME, RUNS_PREFIX
from meta_check import MetaError, check_meta


def read_runs(runs: Path) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    """Cada hash é chaveado pelo `run_id` do `meta.json` ao lado dele, e não pelo nome do
    diretório: é o `run_id` que o plano carrega e pelo qual o núcleo pergunta.
    """
    metas: list[dict[str, Any]] = []
    hashes: dict[str, str] = {}
    warnings: list[str] = []
    for run_dir in sorted(path for path in runs.iterdir() if path.is_dir()):
        key = f"{RUNS_PREFIX}{run_dir.name}/{RUN_META_FILENAME}"
        meta_path = run_dir / RUN_META_FILENAME
        if not meta_path.is_file():
            warnings.append(f"{key}: ausente, Execução ignorada")
            continue
        try:
            meta = check_meta(meta_path.read_bytes())
        except MetaError as error:
            raise MetaError(f"{key}: {error}") from error
        metas.append(meta)
        hash_path = run_dir / RUN_HASH_FILENAME
        if hash_path.is_file():
            # `read_text` levantaria `UnicodeDecodeError` — que não é `OSError`, e
            # que nenhum dos dois CLIs pega — em vez de deixar o núcleo recusar o
            # hash malformado nomeando o run.
            hashes[meta["run_id"]] = hash_path.read_bytes().decode("utf-8", "replace").strip()
    return metas, hashes, warnings
