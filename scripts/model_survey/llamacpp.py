"""What llama.cpp the product builds, what upstream has released, and which
GGUF architectures each of them can load."""

from __future__ import annotations

import re
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO / "macos" / "scripts" / "build-llama.sh"
MIN_ARCHITECTURES = 50  # v0.4.1 has 151
RAW = "https://raw.githubusercontent.com/ggml-org/llama.cpp/{ref}/src/llama-arch.cpp"


def pinned_tag(script_text: str | None = None) -> str:
    text = BUILD_SCRIPT.read_text() if script_text is None else script_text
    m = re.search(r'LLAMA_CPP_TAG="\$\{LLAMA_CPP_TAG:-([^}"]+)\}"', text)
    if not m:
        raise ValueError("could not find the LLAMA_CPP_TAG default in build-llama.sh")
    return m.group(1)


def parse_architectures(arch_cpp: str) -> set[str]:
    """Architecture names from llama-arch.cpp's LLM_ARCH_NAMES table."""
    return set(re.findall(r'\{\s*LLM_ARCH_[A-Z0-9_]+\s*,\s*"([a-z0-9_.-]+)"\s*\}', arch_cpp))


def architectures_at(ref: str, client: httpx.Client) -> set[str]:
    """Raises rather than return a near-empty set: if upstream reshapes the
    table, an empty parse would screen every release as unloadable and give
    every curated model a finding."""
    r = client.get(RAW.format(ref=ref))
    r.raise_for_status()
    found = parse_architectures(r.text)
    if len(found) < MIN_ARCHITECTURES:
        raise ValueError(
            f"parsed {len(found)} architectures from llama-arch.cpp at {ref}; upstream has probably reshaped the table"
        )
    return found


def latest_release(client: httpx.Client) -> dict[str, str]:
    r = client.get("https://api.github.com/repos/ggml-org/llama.cpp/releases/latest")
    r.raise_for_status()
    data = r.json()
    return {"tag": data["tag_name"], "published": data["published_at"][:10]}
