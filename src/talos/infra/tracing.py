"""🔭 OpenTelemetry tracing — GenAI semantic conventions.

2026's observability standard: agent / tool / model operations emit OTel
spans following the GenAI semantic conventions (gen_ai.* attributes), which
Datadog, New Relic, Grafana etc. understand natively — no vendor SDK.

Talos keeps this OFF unless ``talos chat --trace`` (or TALOS_TRACE=true).
When off, ``span()`` is a no-op context manager with zero overhead. When
on, spans go to the console exporter by default, or to an OTLP collector
via the standard OTEL_EXPORTER_OTLP_ENDPOINT env var.
"""

from contextlib import contextmanager

from talos.config import settings

_tracer = None
_enabled = False


def init_tracing() -> bool:
    """Set up the tracer if tracing is enabled. Idempotent."""
    global _tracer, _enabled
    if _enabled:
        return True
    if not settings.trace:
        return False
    try:
        import os

        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )

        resource = Resource.create({"service.name": "talos"})
        provider = TracerProvider(resource=resource)

        # OTLP if a collector endpoint is set, else console
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        else:
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("talos")
        _enabled = True
        return True
    except ImportError:
        return False


@contextmanager
def span(name: str, **attrs):
    """A GenAI span context manager. No-op (zero cost) when tracing is off.

    Usage:
        with span("chat", **{"gen_ai.request.model": model}) as s:
            ...
            set_span_attrs(s, **{"gen_ai.usage.input_tokens": n})
    """
    if not _enabled:
        yield None
        return
    with _tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, v)
        yield s


def set_span_attrs(s, **attrs) -> None:
    if s is not None:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, v)
