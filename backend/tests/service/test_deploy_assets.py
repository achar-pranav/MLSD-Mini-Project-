from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_compose_defines_the_whole_service():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    for name in ("redis", "api", "worker", "frontend", "jaeger", "prometheus", "grafana"):
        assert name in services
    assert "python" in " ".join(services["worker"]["command"])
    assert "./data/index:/app/data/index:ro" in services["worker"]["volumes"]
    assert "run_archive:/data/runs" in services["worker"]["volumes"]


def test_loadtest_overlay_points_worker_at_stub():
    overlay = yaml.safe_load((ROOT / "docker-compose.loadtest.yml").read_text(encoding="utf-8"))
    assert overlay["services"]["stub-llm"]["command"][0] == "uvicorn"
    env = overlay["services"]["worker"]["environment"]
    assert env["LLM_BASE_URL"] == "http://stub-llm:9000/v1"
    assert env["LLM_MODEL"] == "stub"


def test_prometheus_scrapes_api_and_scaled_workers():
    config = yaml.safe_load((ROOT / "monitoring" / "prometheus.yml").read_text(encoding="utf-8"))
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    assert jobs["dispute-api"]["static_configs"][0]["targets"] == ["api:8000"]
    assert jobs["dispute-worker"]["dns_sd_configs"][0]["port"] == 9100
    assert config["rule_files"] == ["/etc/prometheus/alerts.yml"]


def test_dashboard_and_alerts_are_valid_files():
    dashboard = json.loads(
        (ROOT / "monitoring" / "grafana" / "dashboards" / "dispute-service.json").read_text(encoding="utf-8")
    )
    assert dashboard["uid"] == "dispute-service"
    assert len(dashboard["panels"]) >= 8
    alerts = yaml.safe_load((ROOT / "monitoring" / "alerts.yml").read_text(encoding="utf-8"))
    assert alerts["groups"][0]["rules"]


def test_image_pins_python_with_faiss_wheels():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in dockerfile
    assert "EMBEDDING_MODEL" in dockerfile
    assert "data/index" in dockerfile
