from scripts.test_corpora.runner.sampler import CompletionEvent, Sampler


def make_event(idx: int) -> CompletionEvent:
    return CompletionEvent(
        phase=4,
        corpus="cuad",
        model="qwen3-8b",
        question_id=f"q{idx}",
        baseline_answer="Most contracts use 30/60/90 day windows.",
        model_answer="Found 30/60/90 day patterns.",
        citation_overlap=0.9,
        citation_extra=2,
        entity_overlap=0.82,
        latency_seconds=412.0,
        elapsed_total_seconds=2533,
    )


def test_sampler_prints_every_nth(capsys):
    s = Sampler(every_n=3, out=None)
    for i in range(10):
        s.note(make_event(i))
    out = capsys.readouterr().out
    # 10 events with every_n=3 → events 3, 6, 9 printed → 3 sample cards
    assert out.count("[Phase 4 ·") == 3


def test_sampler_summary_table(capsys):
    s = Sampler(every_n=10000, out=None)  # disable in-flight cards
    for model in ("qwen3-8b", "gemma-26b"):
        for i in range(8):
            ev = make_event(i)
            ev.model = model
            ev.citation_overlap = 1.0 if model == "gemma-26b" else 0.5
            s.note(ev)
    s.print_summary_table(phase=4)
    out = capsys.readouterr().out
    assert "Phase 4 complete" in out
    assert "qwen3-8b" in out
    assert "gemma-26b" in out
