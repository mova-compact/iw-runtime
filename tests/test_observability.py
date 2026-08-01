import io
import json
import urllib.error
import urllib.request

from runtime.observability import (
    HealthRegistry, MetricsRegistry, Observability, StructuredLogger,
    configure_runtime_health,
)
from runtime.secrets import register_secret


def test_structured_log_is_json_correlated_and_redacted():
    stream = io.StringIO()
    secret = "observability-secret-value"
    register_secret(secret)
    obs = Observability(logger=StructuredLogger(stream))
    with obs.span("test", run_id="run-1") as context:
        obs.event("work", message=f"failed using {secret}")
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    work = next(record for record in records if record["event"] == "work")
    assert work["run_id"] == "run-1"
    assert work["trace_id"] == context["trace_id"]
    assert secret not in stream.getvalue()


def test_metrics_render_prometheus_labels_and_span_duration():
    metrics = MetricsRegistry()
    obs = Observability(metrics=metrics)
    obs.event("completed", revision="2")
    with obs.span("execution"):
        pass
    rendered = metrics.prometheus()
    assert 'iw_events_total{event="completed",level="info"} 1' in rendered
    assert 'iw_span_duration_seconds_count{span="execution"} 1' in rendered


def test_alert_event_emits_alert_metric_and_log():
    stream = io.StringIO()
    obs = Observability(logger=StructuredLogger(stream))
    obs.event("mechanical_repair_exhausted", level="error")
    assert obs.metrics.get("iw_alerts_total", alert="mechanical_repair_exhausted") == 1
    assert "alert_triggered" in stream.getvalue()


def test_readiness_fails_closed_when_any_check_fails():
    health = HealthRegistry()
    health.register("docker", lambda: True)
    health.register("audit", lambda: {"ok": False})
    status, body = health.ready()
    assert status == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["audit"]["ok"] is False


def test_health_and_metrics_http_endpoints():
    health = HealthRegistry()
    health.register("dependency", lambda: True)
    obs = Observability(health=health)
    obs.metrics.increment("iw_test_total")
    server = obs.serve(port=0)
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        assert json.load(urllib.request.urlopen(base + "/health/live"))["status"] == "live"
        assert json.load(urllib.request.urlopen(base + "/health/ready"))["status"] == "ready"
        assert "iw_test_total 1" in urllib.request.urlopen(base + "/metrics").read().decode()
        try:
            urllib.request.urlopen(base + "/missing")
            assert False
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_runtime_health_includes_docker_llm_and_optional_audit(monkeypatch):
    class Ledger:
        def verify_chain(self):
            return {"ok": True, "entries": 1}

    obs = Observability()
    configure_runtime_health(obs, ledger=Ledger())
    monkeypatch.setattr("runtime.sandbox._docker_available", lambda: True)
    monkeypatch.setattr("runtime.llm_client._provider", lambda: "openai")
    monkeypatch.setattr("runtime.llm_client._model", lambda provider: "model")
    monkeypatch.setattr("runtime.llm_client._api_key", lambda provider: "configured")
    status, body = obs.health.ready()
    assert status == 200
    assert set(body["checks"]) == {"docker", "llm_configuration", "audit_chain"}
