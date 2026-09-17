"""The report generator, offline: JUnit parsing, area mapping, redaction, rendering."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from acceptance import report

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
<testsuite name="pytest" tests="6" time="123.4">
  <testcase classname="acceptance.test_live_core" name="test_a1_health_and_setup_status" time="0.1"/>
  <testcase classname="acceptance.test_live_core" name="test_c2_french_query_reaches_the_french_document" time="0.2">
    <failure message="assert 'french' == 'fr' (password was hunter2-secret)">trace</failure>
  </testcase>
  <testcase classname="acceptance.test_live_core" name="test_d6_search_tier_key_cannot_read_documents_over_rest" time="0.1">
    <skipped type="pytest.xfail" message="#646: API-key tier is enforced only in MCP"/>
  </testcase>
  <testcase classname="acceptance.test_live_lifecycle" name="test_e2_ask_answers_with_citations" time="0.1">
    <skipped type="pytest.skip" message="no local model downloaded on this instance"/>
  </testcase>
  <testcase classname="acceptance.test_live_lifecycle" name="test_h1_soft_deleted_document_leaves_every_retrieval_surface" time="0.3"/>
  <testcase classname="acceptance.test_live_access" name="test_f1_bearer_tool_list_is_exactly_the_tier[search]" time="0.1"/>
  <testcase classname="acceptance.test_live_access" name="test_f1_bearer_tool_list_is_exactly_the_tier[read]" time="0.1"/>
  <testcase classname="acceptance.test_live_lifecycle" name="test_h1c_rest_passages_read_refuses_a_deleted_chunk" time="0.0">
    <skipped type="pytest.xfail" message="#621 follow-up"/>
  </testcase>
</testsuite>
</testsuites>
"""


@pytest.fixture
def junit(tmp_path: Path) -> Path:
    p = tmp_path / "junit.xml"
    p.write_text(JUNIT)
    return p


def test_parse_junit_classifies_every_outcome(junit: Path) -> None:
    summary = report.parse_junit(junit)
    by_id = {c.id: c for c in summary.checks}
    assert by_id["a1"].outcome == "passed"
    assert by_id["c2"].outcome == "failed" and "hunter2-secret" in by_id["c2"].message
    assert by_id["d6"].outcome == "xfailed"
    assert by_id["e2"].outcome == "skipped" and "no local model" in by_id["e2"].message
    assert by_id["h1"].outcome == "passed"
    assert by_id["f1[search]"].outcome == "passed" and by_id["f1[read]"].outcome == "passed", "params stay distinct"
    assert by_id["h1c"].outcome == "xfailed", "a lettered id (h1c) is an id, not a bare test name"
    assert summary.counts == {"passed": 4, "failed": 1, "xfailed": 2, "skipped": 1}
    assert summary.duration_s == pytest.approx(123.4)


def test_area_results_follow_the_smoke_matrix(junit: Path) -> None:
    areas = report.area_results(report.parse_junit(junit))
    assert areas[1][0] == "pass"  # a1
    assert areas[3][0] == "fail"  # c2 failed, h1 passed, h1c xfailed: a failure wins
    assert {c.id for c in areas[3][1]} == {"c2", "h1", "h1c"}
    assert areas[4][0] == "partial"  # d6 xfailed
    assert areas[5][0] == "partial"  # e2 skipped
    assert areas[6][0] == "pass"  # f1
    assert areas[2][0] == "not run" and areas[8][0] == "not run" and areas[9][0] == "not run"


def test_h_checks_map_to_retrieval_and_ingest_areas() -> None:
    assert report.area_of("h1") == 3 and report.area_of("h2") == 2
    assert report.area_of("h1b") == 3 and report.area_of("h1c") == 3, "lettered sub-checks follow their parent"
    assert report.area_of("f1[search]") == 6
    assert report.area_of("zz9") is None


def test_redaction_removes_credentials_and_nothing_else() -> None:
    env = {
        "HC_PASSWORD": "hunter2-secret",
        "HC_USERNAME": "alex@example.com",
        "HC_API_BASE": "http://localhost:8100",
        "HC_ACCEPTANCE_DISPOSABLE": "true",
        "HC_ACCEPTANCE_INGEST_TIMEOUT": "1800",
        "HC_ACCEPTANCE_RUN_ID": "live16",
        "HC_BOT_TOKEN": "ghp_abcdef",
        "HC_X": "ab",
    }
    text = "login for alex@example.com failed: hunter2-secret at http://localhost:8100; assert x is true; 1800 s; live16 ghp_abcdef ab"
    assert (
        report.redact(text, env)
        == "login for *** failed: *** at http://localhost:8100; assert x is true; 1800 s; live16 *** ab"
    )


def test_render_produces_the_nine_area_table_and_redacts(
    junit: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HC_PASSWORD", "hunter2-secret")
    text = report.render(
        report.parse_junit(junit),
        api_base="http://localhost:8100",
        run_id="live-test",
        repo=Path(__file__).resolve().parents[1],
        instance_build="abc1234",
        model="qwen3-8b",
        deployment="macOS native",
        now=dt.datetime(2026, 9, 17, 12, 0, tzinfo=dt.UTC),
    )
    assert "hunter2-secret" not in text and "***" in text
    for area in report.AREAS:
        assert f"| {area} |" in text
    assert "| Search and Find All | fail | c2 ✗, h1 ✓, h1c xfail |" in text
    assert "f1[search] ✓, f1[read] ✓" in text
    assert "| Ask and Research | partial | e2 skip |" in text
    assert "| Recovery and backup docs | not run |" in text
    assert "**c2**" in text and "abc1234" in text and "qwen3-8b" in text and "live-test" in text


def test_main_writes_one_file_per_run_and_never_overwrites(junit: Path, tmp_path: Path, capsys) -> None:
    out_dir = tmp_path / "reports"
    assert report.main(["--junit", str(junit), "--out", str(out_dir), "--api-base", "http://x", "--run-id", "r1"]) == 0
    files = list(out_dir.glob("*-acceptance-*.md"))
    assert len(files) == 1 and files[0].name.startswith(f"{dt.date.today():%Y-%m-%d}-acceptance-")
    assert files[0].name.endswith("-r1.md") and "r1" in files[0].read_text()
    assert report.main(["--junit", str(junit), "--out", str(out_dir), "--api-base", "http://x", "--run-id", "r2"]) == 0
    assert len(list(out_dir.glob("*-acceptance-*.md"))) == 2, "a second run the same day is a second file"
    assert report.main(["--junit", str(junit), "--out", str(out_dir), "--api-base", "http://x", "--run-id", "r1"]) == 2
    assert "refusing to overwrite" in capsys.readouterr().err


def test_main_accepts_an_explicit_md_path_and_records_wipe_mode(junit: Path, tmp_path: Path) -> None:
    out = tmp_path / "custom.md"
    assert (
        report.main(["--junit", str(junit), "--out", str(out), "--api-base", "http://x", "--run-id", "r1", "--wipe"])
        == 0
    )
    text = out.read_text()
    assert "wipe mode" in text and "instance emptied first" in text


def test_suite_commit_marks_a_dirty_tree(tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "f").write_text("a")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "f"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)
    assert not report._git_describe(repo).endswith("-dirty")
    (repo / "f").write_text("b")
    assert report._git_describe(repo).endswith("-dirty"), "a report from uncommitted code must say so"


def test_verify_clean_lists_what_a_run_left(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from acceptance import hc_client

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/login":
            return httpx.Response(200, json={"access_token": "t", "token_type": "bearer", "user": {"role": "admin"}})
        if path == "/api/watch/folders":
            return httpx.Response(
                200, json=[{"folder_id": "f", "path": "/tmp/hc-acceptance-x"}, {"folder_id": "g", "path": "/real"}]
            )
        if path == "/api/api-keys":
            return httpx.Response(
                200,
                json=[{"name": "acceptance-x-g4", "is_active": True}, {"name": "acceptance-x-a3", "is_active": False}],
            )
        if path == "/api/chat/conversations":
            return httpx.Response(200, json=[{"title": "acceptance-x"}, {"title": "mine"}])
        if path == "/api/docs":
            return httpx.Response(200, json={"items": [], "total": 0})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    original = hc_client.HarborClerk.__init__

    def patched(self, base_url, **kw):
        original(self, base_url, transport=transport, **{k: v for k, v in kw.items() if k != "transport"})

    monkeypatch.setattr(hc_client.HarborClerk, "__init__", patched)
    monkeypatch.setenv("HC_USERNAME", "a@b.c")
    monkeypatch.setenv("HC_PASSWORD", "x")
    assert report.verify_clean("http://test", "x") == [
        "watched folder still registered: /tmp/hc-acceptance-x",
        "API key still active: acceptance-x-g4",
        "conversation left: acceptance-x",
    ]


def test_parse_junit_keeps_an_error_that_also_carries_a_skip(tmp_path: Path) -> None:
    """A fixture teardown error on a skipped or xfailed test must not read as a skip."""
    junit = tmp_path / "j.xml"
    junit.write_text(
        """<testsuite name="pytest" tests="2" time="1.0">
  <testcase classname="x" name="test_e2_ask" time="0.1">
    <skipped type="pytest.skip" message="no model"/>
    <error message="teardown failed: folder_delete 500">trace</error>
  </testcase>
  <testcase classname="x" name="test_zz9_future_area" time="0.1"/>
</testsuite>"""
    )
    summary = report.parse_junit(junit)
    by_id = {c.id: c for c in summary.checks}
    assert by_id["e2"].outcome == "error" and "teardown" in by_id["e2"].message
    areas = report.area_results(summary)
    unmapped = [c.id for c in areas[report.UNMAPPED][1]]
    assert unmapped == ["test_zz9_future_area"], "a name with no area keeps its full name and is surfaced, not dropped"
    text = report.render(
        summary,
        api_base="http://x",
        run_id="r",
        repo=Path(__file__).resolve().parents[1],
        instance_build=None,
        model=None,
        deployment="d",
    )
    assert "Unmapped checks" in text and "test_zz9_future_area" in text


def test_probe_instance_reads_build_model_and_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from acceptance import hc_client

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/system/health":
            return httpx.Response(200, json={"status": "healthy", "build": "abc1234"})
        if path == "/api/auth/login":
            return httpx.Response(200, json={"access_token": "t", "token_type": "bearer", "user": {"role": "admin"}})
        if path == "/api/chat/models/status":
            return httpx.Response(200, json={"state": "ready", "model_id": "qwen3-8b"})
        if path == "/api/watch/system":
            return httpx.Response(200, json={"platform": "docker"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    original = hc_client.HarborClerk.__init__

    def patched(self, base_url, **kw):  # route the probe's client through the fake
        original(self, base_url, transport=transport, **{k: v for k, v in kw.items() if k != "transport"})

    monkeypatch.setattr(hc_client.HarborClerk, "__init__", patched)
    monkeypatch.setenv("HC_USERNAME", "a@b.c")
    monkeypatch.setenv("HC_PASSWORD", "x")
    assert report.probe_instance("http://test", None, None, "macOS native") == ("abc1234", "qwen3-8b", "Docker Compose")
