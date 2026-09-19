"""The machine preflight: each check is something that once turned a measurement into a measurement of
something else. Driven by canned command output, so it runs on any machine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.test_corpora import preflight as pf

HEALTHY = {
    "notifyutil": "com.apple.system.thermalpressurelevel 0\n",
    "pmset -g": " lowpowermode         0\n sleep                0\n",
    "pmset -g ps": "Now drawing from 'AC Power'\n",
    "sysctl -n kern.memorystatus_level": "87\n",
    "sysctl -n vm.swapusage": "total = 1024.00M  used = 162.44M  free = 861.56M  (encrypted)\n",
    "sysctl -n hw.memsize": f"{32 * pf.GIB}\n",
    "ioreg": '    "PerformanceStatistics" = {"Device Utilization %"=3,"Renderer Utilization %"=3}\n',
    "ps": "  724  204800 /System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer\n",
    "llama-server": "version: 0.4.1-dev (build 1, commit b29c606)\nbuilt with AppleClang\n",
}


def _machine(changed: dict[str, str | None] | None = None):
    """A fake command runner: the longest key found in the command line wins."""
    outputs = {**HEALTHY, **(changed or {})}

    def run(argv: list[str]) -> str | None:
        line = " ".join(argv)
        for key in sorted(outputs, key=len, reverse=True):
            if key in line:
                return outputs[key]
        return None

    return run


def _check(checks, name):
    return next(c for c in checks if c.name == name)


def _run(tmp_path: Path, run, fetch=lambda url: None, models=("qwen3-8b",), api_base=None):
    binary = tmp_path / "llama-server"
    binary.write_text("")
    return pf.preflight(
        models=list(models), api_base=api_base, llama_server=binary, run=run, fetch=fetch, system="Darwin"
    )


def test_a_healthy_machine_passes_every_check(tmp_path):
    checks = _run(tmp_path, _machine())
    assert pf.verdict(checks) == pf.PASS, [c for c in checks if c.status != pf.PASS]
    assert {c.name for c in checks} >= {
        "thermal pressure",
        "low power mode",
        "power source",
        "memory free",
        "swap in use",
        "GPU at idle",
        "llama-server at the pin",
        "other model servers",
        "fits: qwen3-8b",
    }


@pytest.mark.parametrize(("level", "name"), [(1, "Moderate"), (2, "Heavy"), (4, "Sleeping")])
def test_any_thermal_pressure_fails_the_preflight(tmp_path, level, name):
    """#652: the mini sat at Sleeping for weeks. pmset recorded nothing; this key did."""
    checks = _run(tmp_path, _machine({"notifyutil": f"com.apple.system.thermalpressurelevel {level}\n"}))
    thermal = _check(checks, "thermal pressure")
    assert thermal.status == pf.FAIL and name in thermal.detail and pf.verdict(checks) == pf.FAIL


def test_a_check_that_cannot_look_says_so_and_does_not_pass(tmp_path):
    checks = _run(tmp_path, _machine({"notifyutil": None, "ioreg": None}))
    assert _check(checks, "thermal pressure").status == pf.SKIPPED
    assert _check(checks, "GPU at idle").status == pf.SKIPPED


def test_low_power_mode_and_battery_fail(tmp_path):
    checks = _run(
        tmp_path,
        _machine({"pmset -g": " lowpowermode         1\n", "pmset -g ps": "Now drawing from 'Battery Power'\n"}),
    )
    assert _check(checks, "low power mode").status == pf.FAIL
    assert _check(checks, "power source").status == pf.FAIL


def test_a_machine_with_something_resident_fails_and_heavy_swap_warns(tmp_path):
    busy = {"sysctl -n kern.memorystatus_level": "31\n", "sysctl -n vm.swapusage": "total = 9G  used = 5120.00M  free"}
    checks = _run(tmp_path, _machine(busy))
    memory = _check(checks, "memory free")
    assert memory.status == pf.FAIL and "31%" in memory.detail
    # It names what is resident, because the usual answer is Harbor Clerk's own server from the last run.
    assert "0.2 GB pid 724 WindowServer" in memory.detail and "deactivate the model first" in memory.detail
    assert _check(checks, "swap in use").status == pf.WARN


def test_a_busy_gpu_at_idle_is_a_warning_that_names_the_usual_cause(tmp_path):
    checks = _run(tmp_path, _machine({"ioreg": '"Device Utilization %"=45,'}))
    gpu = _check(checks, "GPU at idle")
    assert gpu.status == pf.WARN and "screensaver" in gpu.detail and pf.verdict(checks) == pf.WARN


def test_a_llama_server_from_before_the_pin_fails(tmp_path):
    """The installed app on the mini reported `version: 1 (c84e6d6)` a day after the pin moved to v0.4.1."""
    checks = _run(tmp_path, _machine({"llama-server": "version: 1 (c84e6d6)\n"}))
    server = _check(checks, "llama-server at the pin")
    assert server.status == pf.FAIL and "c84e6d6" in server.detail and pf.pinned_tag() in server.detail
    missing = pf.check_llama_server(_machine(), tmp_path / "absent")
    assert missing.status == pf.SKIPPED


def test_the_pins_version_is_matched_whole(tmp_path, monkeypatch):
    """HEALTHY's string is what a build of v0.4.1 printed on the mini on 2026-09-18, and `version: 1
    (c84e6d6)` is what the May build printed. 0.4.1 is not 0.4.10, and not 10.4.1."""
    for other in ("version: 0.4.10 (build 1, commit aaaaaaa)\n", "version: 10.4.1\n", "version: 0.4.1.2-dev\n"):
        assert _check(_run(tmp_path, _machine({"llama-server": other})), "llama-server at the pin").status == pf.FAIL
    for same in ("version: 0.4.1-dev (build 1, commit b29c606)\n", "version: 0.4.1\n", "version: v0.4.1 (b29c606)\n"):
        assert _check(_run(tmp_path, _machine({"llama-server": same})), "llama-server at the pin").status == pf.PASS


def test_the_pin_is_read_from_the_build_script():
    assert pf.pinned_tag().startswith("v") and pf.pinned_tag() in (pf.REPO / "docker-compose.yml").read_text()


def test_ollama_holding_a_model_fails_and_an_idle_one_warns(tmp_path):
    ps = HEALTHY["ps"] + " 1847   24576 /opt/homebrew/bin/ollama serve\n"
    held = {"models": [{"name": "qwen3:30b-a3b", "size_vram": 25_671_782_890}]}
    checks = _run(tmp_path, _machine({"ps": ps}), fetch=lambda url: held if "11434" in url else None)
    servers = _check(checks, "other model servers")
    assert servers.status == pf.FAIL and "qwen3:30b-a3b (25.7 GB)" in servers.detail and "bootout" in servers.detail
    idle = _check(_run(tmp_path, _machine({"ps": ps}), fetch=lambda url: {"models": []}), "other model servers")
    assert idle.status == pf.WARN and "pid 1847" in idle.detail
    assert _check(_run(tmp_path, _machine()), "other model servers").status == pf.PASS


def test_a_model_this_machine_cannot_load_fails_by_the_registrys_arithmetic(tmp_path):
    small = _machine({"sysctl -n hw.memsize": f"{16 * pf.GIB}\n"})
    checks = _run(tmp_path, small, models=("qwen3-8b", "qwen36-35b-a3b", "not-a-model"))
    assert _check(checks, "fits: qwen3-8b").status == pf.PASS
    assert _check(checks, "fits: qwen36-35b-a3b").status == pf.FAIL
    assert "registry" in _check(checks, "fits: not-a-model").detail


def test_the_fit_arithmetic_is_the_registrys(tmp_path):
    """Parsed from the registry's source, not copied: a second copy of these numbers would drift."""
    from_source = pf.registry()
    assert set(from_source) >= {"qwen3-8b", "qwen35-9b", "gemma4-26b-a4b"}
    assert from_source["qwen3-8b"] == {"size_bytes": 5_027_783_488, "kv_bytes_per_token": 147_456, "kv_fixed_bytes": 0}
    assert pf._constant("HOST_HEADROOM_BYTES") == 6_000_000_000


def test_a_registry_entry_the_harness_cannot_read_says_which_and_why(tmp_path, monkeypatch):
    """conftest reads the registry at import. A KeyError there kills collection with no hint."""
    import pytest

    fake = tmp_path / "src/harbor_clerk/llm"
    fake.mkdir(parents=True)
    (fake / "models.py").write_text('GIB = 1024**3\nM = [ModelInfo(id="big", size_bytes=5 * GIB)]\n')
    monkeypatch.setattr(pf, "REPO", tmp_path)
    with pytest.raises(ValueError, match=r"line 2: ModelInfo's \['size_bytes'\] must be a literal"):
        pf.registry()


def test_a_certificate_is_only_left_unchecked_on_loopback(monkeypatch):
    import httpx

    seen = {}

    def get(url, timeout, verify):
        seen[url] = verify
        raise httpx.ConnectError("nothing listening")

    monkeypatch.setattr(httpx, "get", get)
    for url in (
        "https://localhost/api/system/health",
        "http://127.0.0.1:11434/api/ps",
        "https://clerk.example.com/api",
    ):
        assert pf.fetch_json(url) is None
    assert seen == {
        "https://localhost/api/system/health": False,
        "http://127.0.0.1:11434/api/ps": False,
        "https://clerk.example.com/api": True,
    }


def test_the_instance_must_be_healthy_when_one_is_named(tmp_path):
    up = _run(tmp_path, _machine(), fetch=lambda url: {"status": "healthy", "build": "1.2.3"}, api_base="http://x")
    assert _check(up, "Harbor Clerk instance").detail == "healthy, build 1.2.3"
    down = _run(tmp_path, _machine(), api_base="http://x")
    assert _check(down, "Harbor Clerk instance").status == pf.FAIL


def test_off_macos_the_machine_checks_are_skipped_not_passed(tmp_path):
    checks = pf.preflight(models=["qwen3-8b"], api_base=None, run=_machine(), fetch=lambda url: None, system="Linux")
    assert _check(checks, "machine state").status == pf.SKIPPED
    assert _check(checks, "fits: qwen3-8b").status == pf.SKIPPED


def test_the_record_carries_the_verdict_host_pin_and_every_check(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pf, "preflight", lambda **kw: [pf.Check("thermal pressure", pf.FAIL, "Heavy")])
    out = tmp_path / "deep" / "preflight.json"
    assert pf.main(["--json", str(out)]) == 1
    record = json.loads(out.read_text())
    assert record["verdict"] == pf.FAIL and record["pinned_llama_cpp"] == pf.pinned_tag()
    assert record["checks"] == [{"name": "thermal pressure", "status": "fail", "detail": "Heavy"}]
    assert "FAIL     thermal pressure: Heavy" in capsys.readouterr().out
    monkeypatch.setattr(pf, "preflight", lambda **kw: [pf.Check("GPU at idle", pf.WARN, "45% busy")])
    assert pf.main([]) == 0, "a warning travels with the report; it does not stop the run"
