"""Where the acceptance suite gets its target and its safety switches.

Everything comes from the environment so credentials never enter the tree;
the `acceptance` skill (PR 3 of the plan) will read them from Keychain. With no `HC_API_BASE` the
whole suite skips, which is why the report in `docs/reports/`, not a green
CI job, is the evidence that it ran.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest

# Wipe mode is refused on any instance holding more documents than this, even
# with the disposable flag: a mistyped URL must not empty the wrong machine.
WIPE_MAX_DOCUMENTS = 500


@dataclass(frozen=True)
class AcceptanceConfig:
    api_base: str
    username: str
    password: str
    folder_root: Path
    folder_root_in_instance: str | None
    insecure: bool
    disposable: bool
    wipe: bool
    keep: bool
    config_json: Path | None  # the native app's config.json, only when the suite may flip enable_cli_access
    allow_model_swap: bool  # may activate a downloaded model when none is active (changes the instance)
    run_id: str
    ingest_timeout_s: int
    ask_timeout_s: int
    model_timeout_s: int  # bound on waiting for llama-server; "loading" can also mean crashed

    @property
    def folder_name(self) -> str:
        return f"hc-acceptance-{self.run_id}"

    @property
    def folder_path(self) -> Path:
        """Where this process writes the fixtures."""
        return self.folder_root / self.folder_name

    @property
    def folder_path_in_instance(self) -> str:
        """The same folder as the instance sees it: identical for the native
        app, the container-side bind-mount path on Compose."""
        return (
            str(Path(self.folder_root_in_instance) / self.folder_name)
            if self.folder_root_in_instance
            else str(self.folder_path)
        )


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def load_config() -> AcceptanceConfig:
    """Build the config or skip the calling test with the reason a human needs."""
    api_base = os.environ.get("HC_API_BASE", "").strip().rstrip("/")
    if not api_base:
        pytest.skip("HC_API_BASE not set: acceptance tests need a running Harbor Clerk instance")
    username = os.environ.get("HC_USERNAME", "").strip()
    password = os.environ.get("HC_PASSWORD", "")
    if not username or not password:
        pytest.skip("HC_USERNAME / HC_PASSWORD not set: acceptance tests need an admin login")

    root = os.environ.get("HC_ACCEPTANCE_FOLDER_ROOT", "").strip()
    folder_root = Path(root).expanduser().resolve() if root else Path(tempfile.gettempdir()).resolve()
    if not folder_root.is_dir():
        pytest.skip(f"HC_ACCEPTANCE_FOLDER_ROOT={folder_root} is not a directory")

    root_in_instance = os.environ.get("HC_ACCEPTANCE_FOLDER_ROOT_IN_INSTANCE", "").strip() or None
    if root_in_instance:
        # The Compose watcher auto-discovers every top-level subdirectory of
        # WATCH_ROOT and, with no unique constraint on the path, registers a
        # second row beside a manually created one: two observers, two
        # documents per file, and a twin that refuses deletion while mounted.
        pytest.skip(
            "Compose targets are not supported yet (watcher auto-discovery creates a twin folder); see the spec"
        )

    wipe = _flag("HC_ACCEPTANCE_WIPE")
    disposable = _flag("HC_ACCEPTANCE_DISPOSABLE")
    if wipe and not disposable:
        pytest.fail("HC_ACCEPTANCE_WIPE=1 requires HC_ACCEPTANCE_DISPOSABLE=1; wipe mode empties the instance")
    if wipe and urlsplit(api_base).hostname not in ("localhost", "127.0.0.1", "::1"):
        pytest.fail(f"HC_ACCEPTANCE_WIPE=1 is only allowed against a loopback instance, not {api_base}")

    config_json_env = os.environ.get("HC_ACCEPTANCE_CONFIG_JSON", "").strip()
    config_json = Path(config_json_env).expanduser() if config_json_env else None
    if config_json is not None and not config_json.is_file():
        pytest.fail(f"HC_ACCEPTANCE_CONFIG_JSON={config_json} is not a file")
    if config_json is not None and urlsplit(api_base).hostname not in ("localhost", "127.0.0.1", "::1"):
        pytest.fail(
            f"HC_ACCEPTANCE_CONFIG_JSON only makes sense for a loopback instance; {api_base} cannot read this file"
        )

    return AcceptanceConfig(
        api_base=api_base,
        username=username,
        password=password,
        folder_root=folder_root,
        folder_root_in_instance=root_in_instance,
        insecure=_flag("HC_INSECURE"),
        disposable=disposable,
        wipe=wipe,
        keep=_flag("HC_ACCEPTANCE_KEEP"),
        config_json=config_json,
        allow_model_swap=disposable or _flag("HC_ACCEPTANCE_ALLOW_MODEL_SWAP"),
        run_id=os.environ.get("HC_ACCEPTANCE_RUN_ID", "").strip() or secrets.token_hex(4),
        ingest_timeout_s=int(os.environ.get("HC_ACCEPTANCE_INGEST_TIMEOUT", "900")),
        ask_timeout_s=int(os.environ.get("HC_ACCEPTANCE_ASK_TIMEOUT", "300")),
        model_timeout_s=int(os.environ.get("HC_ACCEPTANCE_MODEL_TIMEOUT", "120")),
    )
