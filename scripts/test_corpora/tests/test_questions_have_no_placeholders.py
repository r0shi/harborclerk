"""Regression guard: question YAML files must not ship unfilled placeholders.

cuad-ask-1/2/3 in the 2026-05-05-prod sweep shipped ``{{contract_a}}``-style
markers verbatim because the sampler step the YAML's ``notes`` field
described ("fill ``{{contract_a}}`` after sampling") was never implemented.
Each question wasted a Claude baseline call producing a "please clarify"
answer, and each downstream model run was graded against that useless
baseline.

This test loads every question YAML and asserts the ``text`` field is free
of ``{{...}}`` markers — so the next time someone tries to add a templated
question, CI catches it before it ships.

Companion to ``quality.find_unfilled_placeholder``, which provides the
runtime check inside the sweep loop. This test is the build-time check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.test_corpora.runner.quality import find_unfilled_placeholder

_QUESTIONS_DIR = Path(__file__).resolve().parent.parent / "questions"


@pytest.fixture(scope="module")
def all_questions() -> list[tuple[str, str, str]]:
    """Return (corpus, question_id, text) for every question text in every YAML.

    Loaded once per module since the files are tiny and don't change
    between tests in a session.

    Cross-language questions (``cross_language: true`` with a ``variants``
    list) are expanded so each language variant's text gets validated
    separately. The ``id`` for a variant is suffixed with the language tag
    (``synthetic-ask-10__fr``) to match how the sweep refers to them.
    """
    out: list[tuple[str, str, str]] = []
    for path in sorted(_QUESTIONS_DIR.glob("*.yaml")):
        corpus = path.stem
        data = yaml.safe_load(path.read_text())
        for kind in ("research", "ask"):
            for q in data.get(kind) or []:
                if q.get("cross_language") and "variants" in q:
                    for variant in q["variants"]:
                        out.append((corpus, f"{q['id']}__{variant['lang']}", variant["text"]))
                else:
                    out.append((corpus, q["id"], q["text"]))
    return out


def test_question_dir_is_discoverable() -> None:
    # Sanity: we found the questions dir at the expected path.
    assert _QUESTIONS_DIR.is_dir(), f"questions dir not found at {_QUESTIONS_DIR}"
    assert any(_QUESTIONS_DIR.glob("*.yaml"))


def test_question_text_has_no_unfilled_placeholder(all_questions: list[tuple[str, str, str]]) -> None:
    """Every question's ``text`` field must be fully substituted.

    ``find_unfilled_placeholder`` returns the offending placeholder string;
    we surface it in the assertion message so the maintainer immediately
    sees which question is broken.
    """
    failures: list[str] = []
    for corpus, qid, text in all_questions:
        offender = find_unfilled_placeholder(text)
        if offender:
            failures.append(f"{corpus}/{qid}: contains {offender}")
    assert not failures, "Unfilled placeholders in question YAML:\n  " + "\n  ".join(failures)


def test_at_least_one_question_per_kind_per_corpus(all_questions: list[tuple[str, str, str]]) -> None:
    """Smoke check that the fixture parsed real data rather than empty
    YAMLs — catches a corrupted YAML or a missed kind."""
    seen: dict[tuple[str, str], int] = {}
    for corpus, qid, _ in all_questions:
        kind = "research" if "research" in qid else "ask"
        seen[(corpus, kind)] = seen.get((corpus, kind), 0) + 1
    # As of writing, all three corpora have both kinds. If a YAML loses
    # one kind we want to know — the sweep's phase 1 won't generate
    # baselines for kinds that don't exist.
    for (corpus, kind), count in seen.items():
        assert count > 0, f"no {kind} questions for corpus {corpus}"
