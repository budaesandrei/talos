"""Tests for OTel tracing (M38) — focus on the zero-overhead off path."""

from talos import tracing


def test_span_is_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(tracing, "_enabled", False)
    with tracing.span("x", **{"gen_ai.operation.name": "chat"}) as s:
        assert s is None                     # no span object, no cost
    tracing.set_span_attrs(None, foo="bar")  # must not raise


def test_init_returns_false_when_trace_off(monkeypatch):
    monkeypatch.setattr(tracing, "_enabled", False)
    monkeypatch.setattr(tracing.settings, "trace", False)
    assert tracing.init_tracing() is False


def test_init_enables_with_console_exporter(monkeypatch):
    monkeypatch.setattr(tracing, "_enabled", False)
    monkeypatch.setattr(tracing, "_tracer", None)
    monkeypatch.setattr(tracing.settings, "trace", True)
    ok = tracing.init_tracing()
    if ok:  # opentelemetry installed
        with tracing.span("gen_ai.chat") as s:
            tracing.set_span_attrs(s, **{"gen_ai.usage.input_tokens": 5})
    monkeypatch.setattr(tracing, "_enabled", False)  # reset global
