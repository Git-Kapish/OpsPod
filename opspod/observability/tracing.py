"""OpenTelemetry tracing. One trace per case: trace_id == case_id, so logs, audit and Jaeger
all correlate on a single id. Spans cover pipeline steps, skills, tool calls and LLM calls."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import NonRecordingSpan, Span, SpanContext, TraceFlags, set_span_in_context

_PROVIDER = TracerProvider(resource=Resource.create({"service.name": "opspod"}))
trace.set_tracer_provider(_PROVIDER)
_tracer = _PROVIDER.get_tracer("opspod")


def configure_exporter(endpoint: str) -> None:
    """Ship spans to an OTLP collector (Jaeger). Safe to skip: tracing then stays in-process."""
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    _PROVIDER.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )


def add_exporter(exporter: SpanExporter) -> None:
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    _PROVIDER.add_span_processor(SimpleSpanProcessor(exporter))


def case_context(case_id: uuid.UUID) -> Context:
    """Parent context that pins the trace id to the case id."""
    ctx = SpanContext(
        trace_id=case_id.int,
        span_id=int.from_bytes(case_id.bytes[:8], "big") or 1,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return set_span_in_context(NonRecordingSpan(ctx))


@contextmanager
def span(name: str, *, context: Context | None = None, **attrs: Any) -> Iterator[Span]:
    with _tracer.start_as_current_span(name, context=context) as sp:
        for key, value in attrs.items():
            if value is not None:
                sp.set_attribute(key, value)
        yield sp
