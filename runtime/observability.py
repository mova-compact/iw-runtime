"""Redacted structured logs, metrics, tracing, health and alert signals."""

import contextvars
import json
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from .secrets import redact

_trace_id = contextvars.ContextVar("trace_id", default=None)
_span_id = contextvars.ContextVar("span_id", default=None)
_run_id = contextvars.ContextVar("run_id", default=None)


class StructuredLogger:
    def __init__(self, stream=None):
        self.stream = stream
        self._lock = threading.Lock()

    def emit(self, level: str, event: str, **fields) -> dict:
        record = redact({
            "ts": time.time(), "level": level, "event": event,
            "run_id": fields.pop("run_id", None) or _run_id.get(),
            "trace_id": _trace_id.get(), "span_id": _span_id.get(), **fields,
        })
        if self.stream is not None:
            with self._lock:
                self.stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                self.stream.flush()
        return record


class MetricsRegistry:
    def __init__(self):
        self._values = {}
        self._lock = threading.Lock()

    def increment(self, name: str, amount: float = 1, **labels) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def observe(self, name: str, value: float, **labels) -> None:
        self.increment(name + "_count", 1, **labels)
        self.increment(name + "_sum", value, **labels)

    def get(self, name: str, **labels) -> float:
        return self._values.get((name, tuple(sorted(labels.items()))), 0)

    def prometheus(self) -> str:
        lines = []
        with self._lock:
            items = sorted(self._values.items())
        for (name, labels), value in items:
            suffix = ""
            if labels:
                encoded = ",".join(
                    f'{key}="{str(val).replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"'
                    for key, val in labels
                )
                suffix = "{" + encoded + "}"
            lines.append(f"{name}{suffix} {value}")
        return "\n".join(lines) + ("\n" if lines else "")


class HealthRegistry:
    def __init__(self):
        self._checks: dict[str, Callable[[], object]] = {}

    def register(self, name: str, check: Callable[[], object]) -> None:
        self._checks[name] = check

    def live(self) -> dict:
        return {"status": "live"}

    def ready(self) -> tuple[int, dict]:
        checks = {}
        ready = True
        for name, check in self._checks.items():
            try:
                result = check()
                ok = result is True or (isinstance(result, dict) and result.get("ok") is True)
                checks[name] = {"ok": ok}
            except Exception as exc:
                ok = False
                checks[name] = {"ok": False, "error": type(exc).__name__}
            ready = ready and ok
        return (200 if ready else 503), {"status": "ready" if ready else "not_ready", "checks": checks}


class Observability:
    ALERT_EVENTS = {"mechanical_repair_exhausted", "llm_retry_exhausted", "audit_chain_invalid"}

    def __init__(self, logger: Optional[StructuredLogger] = None,
                 metrics: Optional[MetricsRegistry] = None,
                 health: Optional[HealthRegistry] = None):
        self.logger = logger or StructuredLogger()
        self.metrics = metrics or MetricsRegistry()
        self.health = health or HealthRegistry()

    def event(self, event: str, level: str = "info", **fields) -> dict:
        self.metrics.increment("iw_events_total", event=event, level=level)
        record = self.logger.emit(level, event, **fields)
        if event in self.ALERT_EVENTS:
            self.metrics.increment("iw_alerts_total", alert=event)
            self.logger.emit("critical", "alert_triggered", alert=event, source=record)
        return record

    @contextmanager
    def span(self, name: str, run_id: Optional[str] = None, **fields):
        trace_token = None
        if _trace_id.get() is None:
            trace_token = _trace_id.set(uuid.uuid4().hex)
        span_token = _span_id.set(uuid.uuid4().hex[:16])
        run_token = _run_id.set(run_id) if run_id else None
        started = time.monotonic()
        self.event("span_started", span=name, **fields)
        try:
            yield {"trace_id": _trace_id.get(), "span_id": _span_id.get()}
        except Exception as exc:
            self.event("span_failed", level="error", span=name, error_type=type(exc).__name__)
            raise
        finally:
            duration = time.monotonic() - started
            self.metrics.observe("iw_span_duration_seconds", duration, span=name)
            self.event("span_finished", span=name, duration_seconds=duration)
            if run_token is not None:
                _run_id.reset(run_token)
            _span_id.reset(span_token)
            if trace_token is not None:
                _trace_id.reset(trace_token)

    def handler_class(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/health/live":
                    self._json(200, owner.health.live())
                elif self.path == "/health/ready":
                    status, body = owner.health.ready()
                    self._json(status, body)
                elif self.path == "/metrics":
                    payload = owner.metrics.prometheus().encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; version=0.0.4")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self._json(404, {"error": "not_found"})

            def _json(self, status, body):
                payload = json.dumps(body, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                return

        return Handler

    def serve(self, host: str = "127.0.0.1", port: int = 9090) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer((host, port), self.handler_class())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


def configure_runtime_health(observability: Observability, ledger=None) -> None:
    """Register production dependency probes without performing network calls."""
    def docker_ready():
        from .sandbox import _docker_available
        return _docker_available()

    def llm_ready():
        from .llm_client import _api_key, _model, _provider
        provider = _provider()
        _model(provider)
        _api_key(provider)
        return True

    observability.health.register("docker", docker_ready)
    observability.health.register("llm_configuration", llm_ready)
    if ledger is not None:
        observability.health.register("audit_chain", ledger.verify_chain)


default_observability = Observability(
    logger=StructuredLogger(sys.stdout if os.environ.get("IW_LOG_STDOUT") == "1" else None)
)
configure_runtime_health(default_observability)
