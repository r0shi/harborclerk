import subprocess
import sys


def run_cli(*args, env=None):
    """Invoke the CLI as a subprocess and return (stdout, stderr, returncode)."""
    result = subprocess.run(
        [sys.executable, "-m", "harbor_clerk.cli.main", *args],
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout, result.stderr, result.returncode


def test_cli_version_flag():
    out, _err, rc = run_cli("--version")
    assert rc == 0
    assert "harbor-clerk-cli/" in out


def test_cli_help_flag_lists_all_16_commands():
    out, _err, rc = run_cli("--help")
    assert rc == 0
    for cmd in [
        "search",
        "batch-search",
        "read-passages",
        "expand-context",
        "read-document",
        "get-document",
        "list-recent",
        "corpus-overview",
        "document-outline",
        "find-related",
        "entity-search",
        "entity-overview",
        "entity-cooccurrence",
        "ingest-status",
        "reprocess",
        "system-health",
    ]:
        assert cmd in out, f"missing subcommand in --help: {cmd}"


def test_cli_unknown_command_exits_1():
    _out, err, rc = run_cli("totally-fake-subcommand")
    assert rc == 1
    assert "invalid choice" in err.lower() or "unknown" in err.lower()
