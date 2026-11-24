from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROLE_ROOT = Path(__file__).resolve().parent

RUN_SCENARIO = REPO_ROOT / "encode" / "run_scenario.sh"
RUN_ALL = REPO_ROOT / "encode" / "run_all.sh"
FETCH_MASTERS = REPO_ROOT / "encode" / "fetch_masters.sh"
PREPARE_MASTERS = REPO_ROOT / "masters" / "prepare.sh"
RUN_QUALITY = REPO_ROOT / "judge" / "run_quality.sh"
GENERATE_SCENARIOS = REPO_ROOT / "orchestrator" / "generate_scenarios.py"
GENERATE_MASTERS_PLAN = REPO_ROOT / "orchestrator" / "generate_masters_plan.py"
VALIDATE_META = REPO_ROOT / "analysis" / "validate_meta.py"
VALIDATE_JUDGE = REPO_ROOT / "analysis" / "validate_judge.py"
VALIDATE_MANIFEST = REPO_ROOT / "orchestrator" / "validate_manifest.py"
CONSOLIDATE = REPO_ROOT / "analysis" / "consolidate.py"
RESUME = REPO_ROOT / "orchestrator" / "resume.py"
QUALITY_TRIAGE = REPO_ROOT / "orchestrator" / "quality_triage.py"
ORCHESTRATOR = REPO_ROOT / "orchestrator" / "orchestrator.py"
ORCHESTRATOR_DIR = REPO_ROOT / "orchestrator"
EXPERIMENT_TOML = REPO_ROOT / "config" / "experiment.toml"
PILOT_TOML = REPO_ROOT / "config" / "pilot.toml"

COMMIT = "ffd4f43a1b2c3d4e5f60718293a4b5c6d7e8f900"
INSTANCE = "c7g"
INSTANCE_ID = "i-0123456789abcdef0"
INSTANCE_TYPE = "c7g.xlarge"
BUCKET = "smoke-bucket"
MASTERS_PREFIX = "masters/"
MANIFEST_SCHEMA_VERSION = "1"
QUALITY_PLAN_FILENAME = "plan.json"
QUALITY_RESULTS_PREFIX = "quality/results"
RESULTS_DIR_NAME = "results"
JUDGE_FILENAME = "judge.json"
RUNS_SOURCE = f"s3://{BUCKET}/runs/"

# A linha com que a retomada e o triage anunciam um arquivo escrito. O caminho
# nela é a única parte da saída que duas invocações sobre o mesmo bucket não têm
# como ter igual, porque o `--out` é um diretório novo a cada uma.
SLICE_LINE = "fatia reduzida: "
PLAN_LINE = "plano: "

PREFLIGHT_ARTIFACTS = {
    "perf.json": '{"counter-value": "1234567", "event": "cycles"}\n',
    "perf.stderr.txt": "Performance counter stats for 'ffmpeg':\n",
}

# Um cabeçalho de tabela de topo do TOML: `[[codec]]` e `[experiment]`, mas não
# `[video.geometry]` nem `[[instrumentation.metric]]`, que pertencem ao registro
# aberto acima deles.
TOP_LEVEL_TABLE = re.compile(r"^\[\[?[^.\]]+\]\]?$")

VERSIONS = {
    "base_image": "ubuntu:24.04@sha256:33ceb719",
    "ffmpeg": "n8.1.2",
    "libx264": "b35605ace3ddf7c1a5d67a2eb553f034aef41d55",
    "libx265": "4.2",
    "libsvtav1": "v4.1.0",
    "libvmaf": "v3.1.0",
    "libdav1d": "1.5.4",
    "aws_cli": "2.36.38",
}

# O que faz as vezes de um Master no disco e de um source baixado. Nunca um
# vídeo: como o `ffmpeg` é shimado ninguém decodifica isto, e gerá-lo de
# verdade custaria uma dependência de binário no CI.
PLACEHOLDER_BYTES = bytes(range(256)) * 16


def master_bytes(name: str) -> bytes:
    # O nome entra no conteúdo: com seis placeholders idênticos, um `s3 cp` que
    # trouxesse o Master errado casaria o sha256 do manifesto assim mesmo.
    return PLACEHOLDER_BYTES + name.encode("utf-8")


@dataclass(frozen=True)
class ShimTrail:
    argv_dir: Path
    s3_root: Path

    def argv(self, tool: str) -> list[list[str]]:
        raw = (self.argv_dir / f"{tool}.argv").read_text(encoding="utf-8")
        # Cada argumento é **terminado** por NUL, e não separado por ele: o
        # último campo do split é sempre vazio e não é um argumento.
        return [record.split("\0")[:-1] for record in raw.split("\n") if record]

    def sequence(self) -> list[str]:
        return (self.argv_dir / "sequence").read_text(encoding="utf-8").split()

    def encoder_pid(self) -> str:
        return (self.argv_dir / "ffmpeg.pid").read_text(encoding="utf-8").strip()

    def bucket_dir(self) -> Path:
        return self.s3_root / BUCKET

    def object_versions(self, key: str) -> list[bytes]:
        versions = self.argv_dir / "versions" / key
        if not versions.is_dir():
            return []
        return [path.read_bytes() for path in sorted(versions.iterdir())]

    def uploaded(self, run_dir: Path) -> Path:
        return self.bucket_dir() / "runs" / run_dir.name


@dataclass(frozen=True)
class Execution(ShimTrail):
    run: dict[str, Any]
    returncode: int
    stdout: str
    stderr: str
    run_dir: Path

    def meta(self) -> dict[str, Any]:
        return json.loads((self.run_dir / "meta.json").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Preparation(ShimTrail):
    plan: dict[str, Any]
    returncode: int
    stdout: str
    stderr: str
    work_dir: Path

    def manifest_path(self) -> Path:
        return self.work_dir / "manifest.json"

    def manifest(self) -> dict[str, Any]:
        return json.loads(self.manifest_path().read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Loop(ShimTrail):
    plan: dict[str, Any]
    returncode: int
    stdout: str
    stderr: str
    runs_dir: Path

    def run_dirs(self) -> list[Path]:
        return sorted(path for path in self.runs_dir.iterdir() if path.is_dir())

    def metas(self) -> dict[str, dict[str, Any]]:
        return {
            path.name: json.loads((path / "meta.json").read_text(encoding="utf-8"))
            for path in self.run_dirs()
        }

    def encoded_run_ids(self) -> list[str]:
        return [Path(argv[-1]).parent.name for argv in self.argv("ffmpeg") if argv[-1] != "-"]


@dataclass(frozen=True)
class Fetch(ShimTrail):
    manifest: dict[str, Any]
    manifest_path: Path
    returncode: int
    stdout: str
    stderr: str
    dest: Path

    def downloaded(self) -> set[str]:
        return {path.name for path in self.dest.iterdir()}


@dataclass(frozen=True)
class Resume(ShimTrail):
    returncode: int
    stdout: str
    stderr: str
    out_dir: Path

    def slices(self) -> dict[str, dict[str, Any]]:
        return {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.out_dir.iterdir())
        }

    def report_lines(self) -> list[str]:
        return _report_without(self.stdout, SLICE_LINE)


@dataclass(frozen=True)
class Triage(ShimTrail):
    returncode: int
    stdout: str
    stderr: str
    out_dir: Path

    def plan_bytes(self) -> bytes:
        return (self.out_dir / QUALITY_PLAN_FILENAME).read_bytes()

    def plan(self) -> dict[str, Any]:
        return json.loads(self.plan_bytes())

    def report_lines(self) -> list[str]:
        return _report_without(self.stdout, PLAN_LINE)


@dataclass(frozen=True)
class Pass(ShimTrail):
    plan: dict[str, Any]
    returncode: int
    stdout: str
    stderr: str
    work_dir: Path

    def outputs(self) -> list[dict[str, Any]]:
        return self.plan["outputs"]

    def results(self, run_id: str) -> Path:
        return self.bucket_dir() / QUALITY_RESULTS_PREFIX / run_id

    def local_results(self, run_id: str) -> Path:
        return self.work_dir / RESULTS_DIR_NAME / run_id

    def judgement(self, run_id: str) -> dict[str, Any]:
        return json.loads((self.results(run_id) / JUDGE_FILENAME).read_text(encoding="utf-8"))

    def local_output(self, output: dict[str, Any]) -> Path:
        return self.work_dir / f"{output['run_id']}.{output['container']}"


@dataclass(frozen=True)
class Clean(ShimTrail):
    returncode: int
    stdout: str
    stderr: str
    before: dict[str, bytes]

    def objects(self) -> dict[str, bytes]:
        return bucket_objects(self.s3_root)

    def removed(self) -> set[str]:
        return set(self.before) - set(self.objects())


def bucket_objects(s3_root: Path) -> dict[str, bytes]:
    bucket = s3_root / BUCKET
    return {
        path.relative_to(bucket).as_posix(): path.read_bytes()
        for path in bucket.rglob("*")
        if path.is_file()
    }


def _report_without(stdout: str, written_line: str) -> list[str]:
    return [line for line in stdout.splitlines() if not line.startswith(written_line)]


# A forma do marcador de `status/`, que o Juiz repete campo a campo para que o
# leitor do Orquestrador e a decisão de vigilância sirvam aos dois sem ramo.
DONE_MARKER_TYPES = {
    "instance_id": str,
    "finished_at": str,
    "runs_total": int,
    "runs_failed": int,
    "capped": bool,
    "exit_status": int,
}


def typed_fields(payload: dict[str, Any]) -> dict[str, type]:
    return {name: type(value) for name, value in payload.items()}


def offset_aware(timestamp: str) -> bool:
    return datetime.fromisoformat(timestamp).utcoffset() is not None


def keep_first_record(text: str, table: str) -> str:
    kept: list[str] = []
    header = f"[[{table}]]"
    seen = 0
    dropping = False
    for line in text.splitlines(keepends=True):
        if TOP_LEVEL_TABLE.match(line.strip()):
            seen += line.strip() == header
            dropping = line.strip() == header and seen > 1
        if not dropping:
            kept.append(line)
    return "".join(kept)


def block_id_of(scenario_id: str) -> str:
    return scenario_id.rpartition("_")[0]


def failed_block_id(loop: Loop) -> str:
    (failed,) = [meta for meta in loop.metas().values() if meta["exit_code"] != 0]
    return block_id_of(failed["scenario_id"])


def seed_preflight_trail(trail: ShimTrail, instance_id: str) -> None:
    preflight = trail.bucket_dir() / "runs" / "preflight" / instance_id
    preflight.mkdir(parents=True)
    for name, content in PREFLIGHT_ARTIFACTS.items():
        (preflight / name).write_text(content, encoding="utf-8")


def sync_with_the_shim(shim_bin: Path, s3_root: Path, workdir: Path, *filters: str) -> list[str]:
    workdir.mkdir()
    destination = workdir / "down"
    subprocess.run(
        [str(shim_bin / "aws"), "s3", "sync", RUNS_SOURCE, str(destination), *filters],
        env=shim_environment(shim_bin, workdir, {}, s3_root=s3_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(
        str(path.relative_to(destination)) for path in destination.rglob("*") if path.is_file()
    )


def _load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


# Constantes, e não fixtures, porque a matriz de parametrização dos testes sai
# delas.
EXPERIMENT = _load_config(EXPERIMENT_TOML)
PILOT = _load_config(PILOT_TOML)

JUDGE_INSTANCE_TYPE = PILOT["quality"]["judge"]["instance_type"]
JUDGE_INSTANCE_ID = "i-00fedcba9876543210"

# Explícito, e não o default `nproc`: o macOS não o tem, e o número
# viaja para o `n_threads` do `libvmaf`, que é argv asserido.
JUDGE_THREADS = "2"

# As três arquiteturas do `config/pilot.toml`, na ordem em que ele as declara: é
# essa ordem que decide o representante de um bitstream compartilhado.
# O ARM é o que diverge dos dois x86, que é o que o piloto mediu.
ARM = "c7g"
X86 = ("c7i", "c7a")
ARCHITECTURES = (ARM, *X86)

# O bitstream que só o laço do ARM devolve. Os dois x86 ficam com o default do
# shim: é o que faz o hash deles coincidir sem nenhuma combinação entre os laços.
ARM_BITSTREAM = "arm64"


def instance_id_of(architecture: str) -> str:
    return f"i-{ARCHITECTURES.index(architecture):017x}"


def instance_type_of(architecture: str) -> str:
    (record,) = [each for each in PILOT["instance"] if each["id"] == architecture]
    return record["instance_type"]


def bitstream_environment(architecture: str) -> dict[str, str]:
    return {"SMOKE_BITSTREAM": ARM_BITSTREAM} if architecture == ARM else {}


def blocks_of(plan: dict[str, Any], architecture: str) -> list[dict[str, Any]]:
    return [block for block in plan["blocks"] if block["instance"] == architecture]


def generate_plan(config: Path, out_dir: Path) -> dict[str, Any]:
    subprocess.run(
        [
            sys.executable,
            str(GENERATE_SCENARIOS),
            "--config",
            str(config),
            "--out",
            str(out_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads((out_dir / "canonical.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def plan(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return generate_plan(EXPERIMENT_TOML, tmp_path_factory.mktemp("scenarios"))


@pytest.fixture(scope="session")
def pilot_plan(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return generate_plan(PILOT_TOML, tmp_path_factory.mktemp("pilot-scenarios"))


@pytest.fixture(scope="session")
def shim_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    bin_dir = tmp_path_factory.mktemp("bin")
    for shim in sorted((ROLE_ROOT / "shims").glob("*.sh")):
        installed = bin_dir / shim.stem
        shutil.copyfile(shim, installed)
        installed.chmod(0o755)
    return bin_dir


@pytest.fixture(scope="session")
def masters_dir(
    tmp_path_factory: pytest.TempPathFactory,
    plan: dict[str, Any],
    pilot_plan: dict[str, Any],
) -> Path:
    masters = tmp_path_factory.mktemp("masters")
    named = {
        run["master"]
        for definition in (plan, pilot_plan)
        for block in definition["blocks"]
        for run in block["runs"]
    }
    for name in named:
        (masters / name).write_bytes(PLACEHOLDER_BYTES)
    return masters


def _generate_masters_plan() -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(GENERATE_MASTERS_PLAN), "--config", str(EXPERIMENT_TOML)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="session")
def masters_manifest() -> dict[str, Any]:
    plan = _generate_masters_plan()
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "versions": VERSIONS,
        "sources": {video["video"]: video["source"] for video in plan["videos"]},
        "masters": [
            {
                **master,
                "size": len(master_bytes(master["name"])),
                "sha256": hashlib.sha256(master_bytes(master["name"])).hexdigest(),
            }
            for video in plan["videos"]
            for master in (video["master"], *video["derived"])
        ],
    }


@pytest.fixture(scope="session")
def versions_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("image") / "versions.json"
    path.write_text(json.dumps(VERSIONS, indent=2) + "\n", encoding="utf-8")
    return path


def shim_environment(
    shim_bin: Path,
    workdir: Path,
    shim_env: dict[str, str],
    *,
    s3_root: Path | None = None,
) -> dict[str, str]:
    (workdir / "argv").mkdir()
    if s3_root is None:
        s3_root = workdir / "s3"
        s3_root.mkdir()
    return {
        **os.environ,
        "PATH": f"{shim_bin}{os.pathsep}{os.environ['PATH']}",
        "TIME_BIN": str(shim_bin / "time"),
        "SMOKE_ARGV_DIR": str(workdir / "argv"),
        "SMOKE_S3_ROOT": str(s3_root),
        **shim_env,
    }


def bootstrap_arguments(
    masters_dir: Path,
    runs_dir: Path,
    versions_file: Path,
    *,
    instance_id: str = INSTANCE_ID,
    instance_type: str = INSTANCE_TYPE,
) -> list[str]:
    return [
        "--masters-dir",
        str(masters_dir),
        "--runs-dir",
        str(runs_dir),
        "--bucket",
        BUCKET,
        "--commit",
        COMMIT,
        "--instance-id",
        instance_id,
        "--instance-type",
        instance_type,
        "--versions-file",
        str(versions_file),
    ]


@pytest.fixture(scope="session")
def execute(
    tmp_path_factory: pytest.TempPathFactory,
    shim_bin: Path,
    masters_dir: Path,
    versions_file: Path,
):
    def _execute(run: dict[str, Any], **shim_env: str) -> Execution:
        workdir = tmp_path_factory.mktemp("execution")
        env = shim_environment(shim_bin, workdir, shim_env)
        result = subprocess.run(
            [
                "bash",
                str(RUN_SCENARIO),
                "--run",
                json.dumps(run),
                *bootstrap_arguments(masters_dir, workdir / "runs", versions_file),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        # O `run_scenario.sh` anuncia o diretório do run antes de qualquer coisa
        # poder falhar; se nem isso saiu, o que interessa ao diagnóstico é o
        # stderr dele, não um IndexError aqui.
        assert result.stdout.splitlines(), result.stderr
        return Execution(
            run=run,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            run_dir=Path(result.stdout.splitlines()[0]),
            argv_dir=Path(env["SMOKE_ARGV_DIR"]),
            s3_root=Path(env["SMOKE_S3_ROOT"]),
        )

    return _execute


# Um encode do shim leva ~0,5 s; um laço que passe disto está preso, e o que se
# quer ver então é a falha, não um `pytest` pendurado numa hora de `sleep`.
LOOP_DEADLINE_S = 120


@pytest.fixture(scope="session")
def run_all(
    tmp_path_factory: pytest.TempPathFactory,
    shim_bin: Path,
    masters_dir: Path,
    versions_file: Path,
):
    def _run_all(
        plan: dict[str, Any],
        blocks: list[dict[str, Any]],
        *flags: str,
        s3_root: Path | None = None,
        instance: str = INSTANCE,
        instance_id: str = INSTANCE_ID,
        instance_type: str = INSTANCE_TYPE,
        **shim_env: str,
    ) -> Loop:
        workdir = tmp_path_factory.mktemp("loop")
        env = shim_environment(shim_bin, workdir, shim_env, s3_root=s3_root)
        plan_slice = {**plan, "blocks": blocks}
        plan_path = workdir / "slice.json"
        plan_path.write_text(json.dumps(plan_slice), encoding="utf-8")
        runs_dir = workdir / "runs"
        result = subprocess.run(
            [
                "bash",
                str(RUN_ALL),
                "--plan",
                str(plan_path),
                "--instance",
                instance,
                *bootstrap_arguments(
                    masters_dir,
                    runs_dir,
                    versions_file,
                    instance_id=instance_id,
                    instance_type=instance_type,
                ),
                *flags,
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=LOOP_DEADLINE_S,
        )
        return Loop(
            plan=plan_slice,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            runs_dir=runs_dir,
            argv_dir=Path(env["SMOKE_ARGV_DIR"]),
            s3_root=Path(env["SMOKE_S3_ROOT"]),
        )

    return _run_all


@pytest.fixture(scope="session")
def fetch_masters(
    tmp_path_factory: pytest.TempPathFactory,
    shim_bin: Path,
    masters_manifest: dict[str, Any],
):
    def _fetch_masters(*, corrupt: str | None = None, **shim_env: str) -> Fetch:
        workdir = tmp_path_factory.mktemp("fetch")
        env = shim_environment(shim_bin, workdir, shim_env)
        prefix_dir = Path(env["SMOKE_S3_ROOT"]) / BUCKET / MASTERS_PREFIX
        prefix_dir.mkdir(parents=True)
        for master in masters_manifest["masters"]:
            payload = bytearray(master_bytes(master["name"]))
            if master["name"] == corrupt:
                payload[0] ^= 0xFF
            (prefix_dir / master["name"]).write_bytes(payload)

        manifest_path = workdir / "manifest.json"
        manifest_path.write_text(json.dumps(masters_manifest), encoding="utf-8")
        dest = workdir / "masters"
        result = subprocess.run(
            [
                "bash",
                str(FETCH_MASTERS),
                "--manifest",
                str(manifest_path),
                "--bucket",
                BUCKET,
                "--prefix",
                MASTERS_PREFIX,
                "--dest",
                str(dest),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return Fetch(
            manifest=masters_manifest,
            manifest_path=manifest_path,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            dest=dest,
            argv_dir=Path(env["SMOKE_ARGV_DIR"]),
            s3_root=Path(env["SMOKE_S3_ROOT"]),
        )

    return _fetch_masters


@pytest.fixture(scope="session")
def resume(tmp_path_factory: pytest.TempPathFactory, shim_bin: Path):
    def _resume(trail: ShimTrail, config: Path, *flags: str, **shim_env: str) -> Resume:
        workdir = tmp_path_factory.mktemp("resume")
        env = shim_environment(shim_bin, workdir, shim_env, s3_root=trail.s3_root)
        out_dir = workdir / "out"
        result = subprocess.run(
            [
                sys.executable,
                str(RESUME),
                "--config",
                str(config),
                "--bucket",
                BUCKET,
                "--out",
                str(out_dir),
                *flags,
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return Resume(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            out_dir=out_dir,
            argv_dir=Path(env["SMOKE_ARGV_DIR"]),
            s3_root=trail.s3_root,
        )

    return _resume


@pytest.fixture(scope="session")
def quality_triage(tmp_path_factory: pytest.TempPathFactory, shim_bin: Path):
    def _quality_triage(trail: ShimTrail, config: Path) -> Triage:
        workdir = tmp_path_factory.mktemp("triage")
        env = shim_environment(shim_bin, workdir, {}, s3_root=trail.s3_root)
        out_dir = workdir / "out"
        result = subprocess.run(
            [
                sys.executable,
                str(QUALITY_TRIAGE),
                "--config",
                str(config),
                "--bucket",
                BUCKET,
                "--out",
                str(out_dir),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return Triage(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            out_dir=out_dir,
            argv_dir=Path(env["SMOKE_ARGV_DIR"]),
            s3_root=trail.s3_root,
        )

    return _quality_triage


@pytest.fixture(scope="session")
def run_quality(
    tmp_path_factory: pytest.TempPathFactory,
    shim_bin: Path,
    masters_dir: Path,
    versions_file: Path,
):
    def _run_quality(
        trail: ShimTrail,
        plan: dict[str, Any],
        *flags: str,
        **shim_env: str,
    ) -> Pass:
        workdir = tmp_path_factory.mktemp("judge")
        s3_root = workdir / "s3"
        shutil.copytree(trail.s3_root, s3_root)
        env = shim_environment(shim_bin, workdir, shim_env, s3_root=s3_root)
        work_dir = workdir / "work"
        work_dir.mkdir()
        plan_path = work_dir / QUALITY_PLAN_FILENAME
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        result = subprocess.run(
            [
                "bash",
                str(RUN_QUALITY),
                "--plan",
                str(plan_path),
                "--masters-dir",
                str(masters_dir),
                "--work-dir",
                str(work_dir),
                "--bucket",
                BUCKET,
                "--commit",
                COMMIT,
                "--instance-id",
                JUDGE_INSTANCE_ID,
                "--instance-type",
                JUDGE_INSTANCE_TYPE,
                "--versions-file",
                str(versions_file),
                "--threads",
                JUDGE_THREADS,
                *flags,
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=LOOP_DEADLINE_S,
        )
        return Pass(
            plan=plan,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            work_dir=work_dir,
            argv_dir=Path(env["SMOKE_ARGV_DIR"]),
            s3_root=s3_root,
        )

    return _run_quality


# O `clean` não lê nada do arquivo de infra, mas o `orchestrator.py` o exige de
# todo subcomando e o recusa malformado.
INFRA = {
    "subnet_id": "subnet-0123456789abcdef0",
    "security_groups": {"orchestrator": "sg-0a", "ephemeral": "sg-0b"},
    "instance_profiles": {
        "orchestrator": "transcoding-bench-orchestrator",
        "encode": "transcoding-bench-encode",
        "judge": "transcoding-bench-judge",
        "masters": "transcoding-bench-masters",
    },
    "key_pair_name": "transcoding-bench",
    "amis": {"orchestrator": "ami-0a", "encode_amd64": "ami-0b", "encode_arm64": "ami-0c"},
    "buckets": {"campaign": BUCKET, "pilot": BUCKET},
    "ssh_private_key_parameter_name": "/transcoding-bench/orchestrator/ssh-private-key",
}


@pytest.fixture(scope="session")
def clean(tmp_path_factory: pytest.TempPathFactory, shim_bin: Path):
    def _clean(
        trail: ShimTrail,
        plan: Path,
        *flags: str,
        prepare: Callable[[Path], None] | None = None,
        **shim_env: str,
    ) -> Clean:
        workdir = tmp_path_factory.mktemp("clean")
        s3_root = workdir / "s3"
        shutil.copytree(trail.s3_root, s3_root)
        if prepare is not None:
            prepare(s3_root / BUCKET)
        env = shim_environment(shim_bin, workdir, shim_env, s3_root=s3_root)
        infra = workdir / "infra.json"
        infra.write_text(json.dumps(INFRA), encoding="utf-8")
        before = bucket_objects(s3_root)
        result = subprocess.run(
            [
                sys.executable,
                str(ORCHESTRATOR),
                "--infra",
                str(infra),
                "clean",
                "--bucket",
                BUCKET,
                "--plan",
                str(plan),
                *flags,
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return Clean(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            before=before,
            argv_dir=Path(env["SMOKE_ARGV_DIR"]),
            s3_root=s3_root,
        )

    return _clean


@pytest.fixture(scope="session")
def one_codec_toml(tmp_path_factory: pytest.TempPathFactory) -> Path:
    text = keep_first_record(PILOT_TOML.read_text(encoding="utf-8"), "codec")
    path = tmp_path_factory.mktemp("triage-config") / PILOT_TOML.name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(scope="session")
def campaign_plan(tmp_path_factory: pytest.TempPathFactory, one_codec_toml: Path) -> dict[str, Any]:
    return generate_plan(one_codec_toml, tmp_path_factory.mktemp("triage-scenarios"))


@pytest.fixture(scope="session")
def campaign(campaign_plan: dict[str, Any], run_all) -> dict[str, Loop]:
    loops: dict[str, Loop] = {}
    s3_root = None
    for architecture in ARCHITECTURES:
        loops[architecture] = run_all(
            campaign_plan,
            blocks_of(campaign_plan, architecture),
            s3_root=s3_root,
            instance=architecture,
            instance_id=instance_id_of(architecture),
            instance_type=instance_type_of(architecture),
            **bitstream_environment(architecture),
        )
        s3_root = loops[architecture].s3_root
    return loops


@pytest.fixture(scope="session")
def triaged(campaign: dict[str, Loop], one_codec_toml: Path, quality_triage) -> Triage:
    return quality_triage(campaign[ARM], one_codec_toml)


@pytest.fixture(scope="session")
def block(plan: dict[str, Any]) -> dict[str, Any]:
    return plan["blocks"][0]


@pytest.fixture(scope="session")
def loop(plan: dict[str, Any], block: dict[str, Any], run_all) -> Loop:
    return run_all(plan, [block])


SOURCE_SHA256 = hashlib.sha256(PLACEHOLDER_BYTES).hexdigest()
SOURCE_SIZE = len(PLACEHOLDER_BYTES)


@pytest.fixture(scope="session")
def masters_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("source") / "source.bin"
    path.write_bytes(PLACEHOLDER_BYTES)
    return path


def _substitute_once_per_video(text: str, pattern: str, replacement: str) -> str:
    patched, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    assert count == len(EXPERIMENT["video"]), pattern
    return patched


@pytest.fixture(scope="session")
def masters_toml(tmp_path_factory: pytest.TempPathFactory) -> Path:
    text = _substitute_once_per_video(
        EXPERIMENT_TOML.read_text(encoding="utf-8"),
        r'^sha256 = ".*"$',
        f'sha256 = "{SOURCE_SHA256}"',
    )
    text = _substitute_once_per_video(text, r"^size = \d+$", f"size = {SOURCE_SIZE}")
    path = tmp_path_factory.mktemp("masters-config") / "experiment.toml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(scope="session")
def masters_plan(tmp_path_factory: pytest.TempPathFactory, masters_toml: Path) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("masters-plan") / "masters.json"
    subprocess.run(
        [
            sys.executable,
            str(GENERATE_MASTERS_PLAN),
            "--config",
            str(masters_toml),
            "--out",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(out.read_text(encoding="utf-8"))


def masters_of(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        master
        for video in plan["videos"]
        for master in (video["master"], *video.get("derived", ()))
    ]


def probe_response(master: dict[str, Any]) -> dict[str, Any]:
    return {
        "streams": [
            {
                "codec_name": master["codec_name"],
                "width": master["width"],
                "height": master["height"],
                "pix_fmt": master["pix_fmt"],
                "r_frame_rate": master["frame_rate"],
                "nb_read_packets": str(master["frames"]),
            }
        ]
    }


@pytest.fixture(scope="session")
def prepare(
    tmp_path_factory: pytest.TempPathFactory,
    shim_bin: Path,
    masters_source: Path,
    versions_file: Path,
):
    def _prepare(
        plan: dict[str, Any],
        probes: dict[str, dict[str, Any]] | None = None,
        **shim_env: str,
    ) -> Preparation:
        workdir = tmp_path_factory.mktemp("preparation")
        probe_dir = workdir / "probes"
        probe_dir.mkdir()
        responses = {master["name"]: probe_response(master) for master in masters_of(plan)}
        responses.update(probes or {})
        for name, response in responses.items():
            (probe_dir / f"{name}.json").write_text(json.dumps(response), encoding="utf-8")

        env = shim_environment(
            shim_bin,
            workdir,
            {
                "SMOKE_SOURCE_FILE": str(masters_source),
                "SMOKE_PROBE_DIR": str(probe_dir),
                **shim_env,
            },
        )
        work_dir = workdir / "work"
        result = subprocess.run(
            [
                "bash",
                str(PREPARE_MASTERS),
                "--plan",
                json.dumps(plan),
                "--bucket",
                BUCKET,
                "--work-dir",
                str(work_dir),
                "--versions-file",
                str(versions_file),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return Preparation(
            plan=plan,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            work_dir=work_dir,
            argv_dir=Path(env["SMOKE_ARGV_DIR"]),
            s3_root=Path(env["SMOKE_S3_ROOT"]),
        )

    return _prepare


@pytest.fixture(scope="session")
def list_objects(tmp_path_factory: pytest.TempPathFactory, shim_bin: Path):
    def _list_objects(trail: ShimTrail, prefix: str) -> list[str]:
        scratch = tmp_path_factory.mktemp("reader")
        result = subprocess.run(
            [
                str(shim_bin / "aws"),
                "s3api",
                "list-objects-v2",
                "--bucket",
                BUCKET,
                "--prefix",
                prefix,
                "--output",
                "json",
            ],
            env={
                **os.environ,
                "SMOKE_ARGV_DIR": str(scratch),
                "SMOKE_S3_ROOT": str(trail.s3_root),
            },
            capture_output=True,
            text=True,
            check=True,
        )
        if not result.stdout.strip():
            return []
        return [item["Key"] for item in json.loads(result.stdout)["Contents"]]

    return _list_objects


def check_with_stdlib_checker(meta_path: Path) -> subprocess.CompletedProcess[str]:
    return _check_with_stdlib_checker(meta_path, "meta_check", "check_meta")


def check_judgement_with_stdlib_checker(judge_path: Path) -> subprocess.CompletedProcess[str]:
    return _check_with_stdlib_checker(judge_path, "judgement_check", "check_judgement")


def _check_with_stdlib_checker(
    path: Path, module: str, checker: str
) -> subprocess.CompletedProcess[str]:
    program = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        f"from {module} import {checker};"
        f"{checker}(open(sys.argv[2], 'rb').read())"
    )
    return subprocess.run(
        [sys.executable, "-c", program, str(ORCHESTRATOR_DIR), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def check_with_preflight(perf_json: Path) -> subprocess.CompletedProcess[str]:
    program = (
        "import sys, tomllib; sys.path.insert(0, sys.argv[1]);"
        "from experiment_config import validate_config;"
        "from preflight import perf_counters;"
        "config = validate_config(tomllib.load(open(sys.argv[3], 'rb')));"
        "perf_counters(open(sys.argv[2], encoding='utf-8').read(), config.instrumentation)"
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(ORCHESTRATOR_DIR),
            str(perf_json),
            str(EXPERIMENT_TOML),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def validate_with_cli(meta_path: Path) -> subprocess.CompletedProcess[str]:
    return _validate_with_cli(meta_path, VALIDATE_META)


def validate_judgement_with_cli(judge_path: Path) -> subprocess.CompletedProcess[str]:
    return _validate_with_cli(judge_path, VALIDATE_JUDGE)


def _validate_with_cli(path: Path, cli: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(cli), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def validate_manifest_with_cli(
    manifest_path: Path, config: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATE_MANIFEST), str(manifest_path), "--config", str(config)],
        capture_output=True,
        text=True,
        check=False,
    )


def consolidate_with_cli(
    runs_dir: Path, out: Path, config: Path = EXPERIMENT_TOML
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CONSOLIDATE),
            "--runs",
            str(runs_dir),
            "--config",
            str(config),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


DOCKER_OPTION = "--docker"
CAPTURE_DIR_OPTION = "--capture-dir"
STATUS_CAPTURE_DIR_OPTION = "--status-capture-dir"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        DOCKER_OPTION,
        action="store_true",
        default=False,
        help="roda a camada de aceite: builda a imagem e exercita as ferramentas reais",
    )
    parser.addoption(
        CAPTURE_DIR_OPTION,
        default=None,
        help="diretório onde depositar as saídas cruas do aceite, para virarem fixtures",
    )
    parser.addoption(
        STATUS_CAPTURE_DIR_OPTION,
        default=None,
        help="diretório onde depositar o par de `status/` que o laço escreveu",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", f"docker: camada de aceite dentro da imagem, só coletada com {DOCKER_OPTION}"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption(DOCKER_OPTION):
        return
    deselected = [item for item in items if item.get_closest_marker("docker")]
    if not deselected:
        return
    config.hook.pytest_deselected(items=deselected)
    items[:] = [item for item in items if item.get_closest_marker("docker") is None]


@pytest.fixture(scope="session")
def capture_dir(pytestconfig: pytest.Config) -> Path | None:
    return _requested_dir(pytestconfig, CAPTURE_DIR_OPTION)


@pytest.fixture(scope="session")
def status_capture_dir(pytestconfig: pytest.Config) -> Path | None:
    return _requested_dir(pytestconfig, STATUS_CAPTURE_DIR_OPTION)


def _requested_dir(config: pytest.Config, option: str) -> Path | None:
    requested = config.getoption(option)
    if requested is None:
        return None
    destination = Path(requested).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    return destination
