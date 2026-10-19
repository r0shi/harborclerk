"""Compose's llama-server serves one model, and the app follows the same model and context (#654).

Without `-m`, the pinned llama.cpp (v0.4.1) starts in router mode: `/health` answers 200 with no model
loaded and every chat request the app sends is refused with HTTP 400 ("model name is missing from the
request"), because no payload names a `model`. Nothing failed visibly: chat and research errored,
summaries fell back to extractive, and the container stayed healthy. These are static checks on the
compose file and a behavioural check of the entrypoint that waits for the model file.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from harbor_clerk.llm.models import MODELS, model_for_file

REPO = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPO / "docker-compose.yml"
ENTRYPOINT = REPO / "docker" / "llama-server-entrypoint.sh"
ENTRYPOINT_IN_CONTAINER = "/usr/local/bin/llama-server-entrypoint.sh"

# `-` and not `:-`: an LLM_MODEL_FILE set empty in .env must stay empty (no local model), not fall back.
MODEL_FILE_EXPR = re.compile(r"^\$\{LLM_MODEL_FILE-([^}]+)\}$")
CONTEXT_EXPR = re.compile(r"^\$\{LLM_CONTEXT_TOKENS:-(\d+)\}$")


def _services() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())["services"]


def _llama_args() -> list[str]:
    return [str(token) for token in _services()["llama-server"]["command"]]


def _flag(args: list[str], flag: str) -> str:
    assert flag in args, f"llama-server is started without {flag}: {args}"
    return args[args.index(flag) + 1]


def _default_model_file() -> str:
    model_path = _flag(_llama_args(), "-m")
    assert model_path.startswith("/models/"), model_path
    match = MODEL_FILE_EXPR.match(model_path.removeprefix("/models/"))
    assert match, f"-m must be /models/${{LLM_MODEL_FILE-<default>}}, got {model_path!r}"
    return match.group(1)


def test_llama_server_is_started_with_one_model_not_as_a_router() -> None:
    args = _llama_args()
    assert "--models-dir" not in args and "--models-preset" not in args
    default = _default_model_file()
    model = model_for_file(default)
    assert model is not None, f"the default LLM_MODEL_FILE {default!r} is not a curated model's file"
    assert model.superseded_by is None, f"the default model {model.id} is superseded by {model.superseded_by}"


def test_app_and_llm_worker_are_given_the_same_model_and_context() -> None:
    services = _services()
    args = _llama_args()
    model_expr = _flag(args, "-m").removeprefix("/models/")
    context_expr = _flag(args, "-c")
    assert CONTEXT_EXPR.match(context_expr), f"-c must be ${{LLM_CONTEXT_TOKENS:-<default>}}, got {context_expr!r}"
    for name in ("app", "worker-llm"):
        env = services[name]["environment"]
        # The marker that makes a process Compose: nothing else sets it, so a copied .env cannot (#654).
        assert env.get("HC_DEPLOYMENT") == "compose", name
        assert env.get("LLM_MODEL_FILE") == model_expr, name
        assert env.get("LLM_CONTEXT_TOKENS") == context_expr, name
        # The model counts as active only while its file is in the volume, so both must see it there.
        assert env.get("MODELS_DIR") == "/models", name
        volumes = [str(v) for v in services[name].get("volumes") or []]
        assert any(v.split(":")[:2] == ["llama_models", "/models"] for v in volumes), (name, volumes)


def test_one_slot_as_every_curated_model_runs() -> None:
    # -c is the total across slots; the app budgets a request to -c / parallel_slots.
    assert {m.parallel_slots for m in MODELS.values()} == {1}
    assert _flag(_llama_args(), "-np") == "1"


def test_entrypoint_is_the_waiting_script() -> None:
    service = _services()["llama-server"]
    assert service["entrypoint"] == ["/bin/sh", ENTRYPOINT_IN_CONTAINER]
    assert service.get("init") is True  # SIGTERM reaches the shell, which also traps it
    assert f"./docker/llama-server-entrypoint.sh:{ENTRYPOINT_IN_CONTAINER}:ro" in service["volumes"]
    # Healthy means a model is loaded: single-model /health is 200 only after the load.
    assert service["healthcheck"]["test"] == ["CMD", "curl", "-sf", "http://localhost:8080/health"]


def test_env_example_names_the_compose_defaults_commented_out() -> None:
    # Commented out: Settings reads .env from the working directory, and the compose file's defaults
    # already cover these, so the copy the Docker quick start makes sets nothing a source run would read.
    lines = (REPO / ".env.example").read_text().splitlines()
    assert not [ln for ln in lines if re.match(r"\s*(LLM_MODEL_FILE|LLM_CONTEXT_TOKENS|HC_DEPLOYMENT)\s*=", ln)]
    values = dict(ln.removeprefix("# ").split("=", 1) for ln in lines if re.match(r"# LLM_[A-Z_]+=", ln))
    assert values["LLM_MODEL_FILE"] == _default_model_file()
    assert values["LLM_CONTEXT_TOKENS"] == CONTEXT_EXPR.match(_flag(_llama_args(), "-c")).group(1)


# --- the entrypoint itself ---

needs_sh = pytest.mark.skipif(shutil.which("sh") is None or sys.platform == "win32", reason="needs a POSIX sh")


def _fake_server(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in for /app/llama-server that records the arguments it was exec'd with."""
    out = tmp_path / "exec-args"
    fake = tmp_path / "fake-llama-server"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{out}"\n')
    fake.chmod(0o755)
    return fake, out


def _env(fake: Path) -> dict[str, str]:
    return {
        **os.environ,
        "LLAMA_SERVER_BIN": str(fake),
        "LLAMA_WAIT_INTERVAL": "0.05",
        "LLAMA_STABLE_INTERVAL": "0.05",
    }


@needs_sh
def test_entrypoint_execs_llama_server_with_its_arguments_when_the_model_is_there(tmp_path: Path) -> None:
    fake, out = _fake_server(tmp_path)
    model = tmp_path / "Qwen3.5-4B-Q4_K_M.gguf"
    model.write_bytes(b"GGUF")
    args = ["-m", str(model), "--host", "0.0.0.0", "-c", "8192", "--alias", "two words"]
    result = subprocess.run(["sh", str(ENTRYPOINT), *args], env=_env(fake), capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert out.read_text().splitlines() == args


@needs_sh
def test_entrypoint_refuses_to_start_a_router(tmp_path: Path) -> None:
    fake, out = _fake_server(tmp_path)
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), "--host", "0.0.0.0", "--port", "8080"], env=_env(fake), capture_output=True, timeout=10
    )
    assert result.returncode == 64
    assert b"router mode" in result.stderr
    assert not out.exists()


@needs_sh
def test_entrypoint_waits_for_the_model_file_then_starts(tmp_path: Path) -> None:
    fake, out = _fake_server(tmp_path)
    model = tmp_path / "Qwen3.5-4B-Q4_K_M.gguf"
    proc = subprocess.Popen(
        ["sh", str(ENTRYPOINT), "-m", str(model), "--port", "8080"],
        env=_env(fake),
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(0.5)
        assert proc.poll() is None, "the entrypoint exited instead of waiting for the model file"
        assert not out.exists(), "llama-server was started before its model file existed"
        model.write_bytes(b"GGUF")
        assert proc.wait(timeout=10) == 0
        assert out.read_text().splitlines() == ["-m", str(model), "--port", "8080"]
        assert b"waiting for" in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@needs_sh
def test_entrypoint_says_no_model_is_configured_when_the_file_name_is_empty(tmp_path: Path) -> None:
    fake, out = _fake_server(tmp_path)
    # What `-m /models/${LLM_MODEL_FILE-...}` renders to when LLM_MODEL_FILE is set empty.
    proc = subprocess.Popen(
        ["sh", str(ENTRYPOINT), "-m", f"{tmp_path}/", "--port", "8080"],
        env=_env(fake),
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(0.5)
        assert proc.poll() is None
        assert not out.exists()
    finally:
        proc.kill()
        proc.wait(timeout=5)
    assert b"LLM_MODEL_FILE is empty" in proc.stderr.read()


@needs_sh
def test_entrypoint_waits_out_a_file_that_is_not_a_gguf_yet(tmp_path: Path) -> None:
    fake, out = _fake_server(tmp_path)
    model = tmp_path / "Qwen3.5-4B-Q4_K_M.gguf"
    model.write_bytes(b"\0\0\0\0 a copy caught before its header")
    proc = subprocess.Popen(["sh", str(ENTRYPOINT), "-m", str(model)], env=_env(fake), stderr=subprocess.PIPE)
    try:
        time.sleep(0.6)
        assert proc.poll() is None, "the entrypoint started llama-server on a file with no GGUF magic"
        assert not out.exists()
        model.write_bytes(b"GGUF" + b"\0" * 64)
        assert proc.wait(timeout=10) == 0
        assert out.read_text().splitlines() == ["-m", str(model)]
        assert b"is not a whole GGUF yet" in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@needs_sh
def test_entrypoint_waits_while_the_file_is_still_growing(tmp_path: Path) -> None:
    fake, out = _fake_server(tmp_path)
    model = tmp_path / "Qwen3.5-4B-Q4_K_M.gguf"
    model.write_bytes(b"GGUF")
    env = {**_env(fake), "LLAMA_STABLE_INTERVAL": "0.3"}
    proc = subprocess.Popen(["sh", str(ENTRYPOINT), "-m", str(model)], env=env, stderr=subprocess.PIPE)
    try:
        # A copy in progress under the final name: the size changes within every stability interval.
        deadline = time.monotonic() + 1.5
        with model.open("ab") as f:
            while time.monotonic() < deadline:
                f.write(b"\0" * 1024)
                f.flush()
                time.sleep(0.05)
                assert proc.poll() is None, "the entrypoint started llama-server on a file still being written"
        assert not out.exists()
        assert proc.wait(timeout=10) == 0, "the entrypoint did not start once the file stopped growing"
        assert out.read_text().splitlines() == ["-m", str(model)]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@needs_sh
def test_entrypoint_stops_at_once_on_sigterm_while_waiting(tmp_path: Path) -> None:
    fake, out = _fake_server(tmp_path)
    # A long poll interval: the trap must interrupt the backgrounded sleep, not wait it out, so shutdown
    # is prompt. Exit 143 is the trap's; an untrapped shell would die of the signal itself (-15).
    env = {**_env(fake), "LLAMA_WAIT_INTERVAL": "30"}
    proc = subprocess.Popen(["sh", str(ENTRYPOINT), "-m", str(tmp_path / "absent.gguf")], env=env)
    try:
        time.sleep(0.5)
        assert proc.poll() is None
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 143
        assert time.monotonic() - started < 3
        assert not out.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@needs_sh
@pytest.mark.parametrize("form", ["--model=", "LLAMA_ARG_MODEL"])
def test_entrypoint_reads_the_other_ways_llama_server_takes_a_model(tmp_path: Path, form: str) -> None:
    # llama-server also takes `--model=PATH`, and LLAMA_ARG_MODEL from the environment: either is a model
    # given, to wait for, not a router to refuse.
    fake, out = _fake_server(tmp_path)
    model = tmp_path / "Qwen3.5-4B-Q4_K_M.gguf"
    model.write_bytes(b"GGUF")
    env = _env(fake)
    if form == "--model=":
        args = [f"--model={model}", "--port", "8080"]
    else:
        args = ["--port", "8080"]
        env["LLAMA_ARG_MODEL"] = str(model)
    result = subprocess.run(["sh", str(ENTRYPOINT), *args], env=env, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert out.read_text().splitlines() == args


@needs_sh
def test_entrypoint_waits_for_a_model_given_with_model_equals(tmp_path: Path) -> None:
    fake, out = _fake_server(tmp_path)
    model = tmp_path / "Qwen3.5-4B-Q4_K_M.gguf"
    proc = subprocess.Popen(["sh", str(ENTRYPOINT), f"--model={model}"], env=_env(fake), stderr=subprocess.PIPE)
    try:
        time.sleep(0.5)
        assert proc.poll() is None and not out.exists(), "started before the model file existed"
        model.write_bytes(b"GGUF")
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@needs_sh
def test_entrypoint_names_a_model_file_ending_in_a_slash(tmp_path: Path) -> None:
    # LLM_MODEL_FILE=name.gguf/ renders -m /models/name.gguf/: a directory. The message names it, so the operator
    # sees which value is wrong (the app refuses the same value: tests/test_compose_model_file.py).
    fake, out = _fake_server(tmp_path)
    bad = f"{tmp_path}/Qwen3.5-4B-Q4_K_M.gguf/"
    proc = subprocess.Popen(
        ["sh", str(ENTRYPOINT), "-m", bad, "--port", "8080"], env=_env(fake), stderr=subprocess.PIPE
    )
    try:
        time.sleep(0.5)
        assert proc.poll() is None and not out.exists()
    finally:
        proc.kill()
        proc.wait(timeout=5)
    err = proc.stderr.read()
    assert b"Qwen3.5-4B-Q4_K_M.gguf/ is a directory" in err and b"ends in '/'" in err, err


def test_the_compose_default_context_is_the_apps() -> None:
    # The app falls back to the compose default when LLM_CONTEXT_TOKENS is 0; it must be the same number.
    from harbor_clerk.config import COMPOSE_DEFAULT_CONTEXT_TOKENS

    assert int(CONTEXT_EXPR.match(_flag(_llama_args(), "-c")).group(1)) == COMPOSE_DEFAULT_CONTEXT_TOKENS


needs_dash = pytest.mark.skipif(shutil.which("dash") is None, reason="needs dash, the /bin/sh of Debian images")


@needs_dash
@pytest.mark.parametrize(
    ("given", "passed"),
    [
        (["-c", "0"], ["-c", "8192"]),
        (["--ctx-size", "0"], ["--ctx-size", "8192"]),
        (["-c=0"], ["-c=8192"]),
        (["--ctx-size=0"], ["--ctx-size=8192"]),
        # llama-server parses `00` and `000` as 0 as well (LLM_CONTEXT_TOKENS=00): the comparison is numeric.
        (["-c", "00"], ["-c", "8192"]),
        (["--ctx-size", "000"], ["--ctx-size", "8192"]),
        (["-c=00"], ["-c=8192"]),
        (["--ctx-size=000"], ["--ctx-size=8192"]),
    ],
)
def test_entrypoint_replaces_a_zero_context_with_the_compose_default(tmp_path: Path, given, passed) -> None:
    # LLM_CONTEXT_TOKENS=0 renders `-c 0`, which llama-server reads as the full training context: an OOM loop in a
    # container. The entrypoint passes the compose default instead, as the app budgets, and says why.
    fake, out = _fake_server(tmp_path)
    model = tmp_path / "Qwen3.5-4B-Q4_K_M.gguf"
    model.write_bytes(b"GGUF")
    args = ["-m", str(model), "-ngl", "0", *given, "--port", "8080"]
    result = subprocess.run(["dash", str(ENTRYPOINT), *args], env=_env(fake), capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert out.read_text().splitlines() == ["-m", str(model), "-ngl", "0", *passed, "--port", "8080"]
    value = given[-1].split("=")[-1]
    assert f"LLM_CONTEXT_TOKENS='{value}' is zero".encode() in result.stderr


# What llama-server's std::stoi would misread (#654): `8k` as 8 (padded to 256 tokens), `16,384` as 16, `-1` wraps
# and crashes, `+0`, `-0`, `0.0` and `0x0` as 0 (the full training context), `abc` throws. Non-ASCII digits are not
# digits to it either.
MALFORMED_CONTEXTS = ["8k", "16,384", "-1", "+0", "-0", "0.0", "0x0", "abc", "\u00b2", "\u0668\u0661\u0669\u0662", ""]


@needs_dash
@pytest.mark.parametrize("form", ["spaced", "equals"])
@pytest.mark.parametrize("value", MALFORMED_CONTEXTS)
def test_entrypoint_replaces_a_malformed_context_with_the_compose_default(
    tmp_path: Path, value: str, form: str
) -> None:
    # The app logs that the compose default applies to a malformed LLM_CONTEXT_TOKENS; llama-server must get it too.
    fake, out = _fake_server(tmp_path)
    model = tmp_path / "Qwen3.5-4B-Q4_K_M.gguf"
    model.write_bytes(b"GGUF")
    given, passed = (
        (["-c", value], ["-c", "8192"]) if form == "spaced" else ([f"--ctx-size={value}"], ["--ctx-size=8192"])
    )
    args = ["-m", str(model), *given, "--port", "8080"]
    result = subprocess.run(["dash", str(ENTRYPOINT), *args], env=_env(fake), capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert out.read_text().splitlines() == ["-m", str(model), *passed, "--port", "8080"]
    assert f"LLM_CONTEXT_TOKENS='{value}' is not a whole number of tokens".encode() in result.stderr


@needs_dash
@pytest.mark.parametrize("context", [["-c", "16384"], ["-c", "010"], ["--ctx-size=08192"], ["--ctx-size", "4096"]])
def test_entrypoint_leaves_a_real_context_alone(tmp_path: Path, context) -> None:
    # A non-zero context passes untouched, leading zeros and all; so do other flags whose value is 0.
    fake, out = _fake_server(tmp_path)
    model = tmp_path / "Qwen3.5-4B-Q4_K_M.gguf"
    model.write_bytes(b"GGUF")
    args = ["-m", str(model), "-ngl", "0", *context, "--cache-ram", "0"]
    result = subprocess.run(["dash", str(ENTRYPOINT), *args], env=_env(fake), capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert out.read_text().splitlines() == args
    assert b"LLM_CONTEXT_TOKENS" not in result.stderr


def test_the_entrypoints_default_context_is_the_apps() -> None:
    from harbor_clerk.config import COMPOSE_DEFAULT_CONTEXT_TOKENS

    match = re.search(r"^COMPOSE_DEFAULT_CONTEXT=(\d+)$", ENTRYPOINT.read_text(), re.M)
    assert match and int(match.group(1)) == COMPOSE_DEFAULT_CONTEXT_TOKENS


@needs_sh
def test_sigterm_while_waiting_leaves_no_sleep_behind(tmp_path: Path) -> None:
    # The wait sleeps in the background so a trap can interrupt it; the trap must also kill that sleep, or every
    # `docker compose down` during a wait leaves an orphan for the length of the poll interval.
    fake, out = _fake_server(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pids = tmp_path / "sleep-pids"
    real_sleep = shutil.which("sleep")
    fake_sleep = bin_dir / "sleep"
    fake_sleep.write_text(f'#!/bin/sh\necho $$ >> "{pids}"\nexec {real_sleep} "$@"\n')
    fake_sleep.chmod(0o755)
    env = {**_env(fake), "LLAMA_WAIT_INTERVAL": "30", "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    proc = subprocess.Popen(["sh", str(ENTRYPOINT), "-m", str(tmp_path / "absent.gguf")], env=env)
    sleepers: list[int] = []
    try:
        deadline = time.monotonic() + 5
        while not (pids.exists() and pids.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.05)
        sleepers = [int(p) for p in pids.read_text().split()]
        assert sleepers, "the entrypoint never started its wait"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 143

        def alive(pid: int) -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            return True

        deadline = time.monotonic() + 3
        while any(alive(p) for p in sleepers) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not [p for p in sleepers if alive(p)], "SIGTERM left the wait's sleep running"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        for p in sleepers:
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass
