from __future__ import annotations

import http.client
import json
from pathlib import Path
import threading
from contextlib import contextmanager

import pytest

from charon.graph_studio_server import (
    CONTROL_HEADER,
    GraphStudioController,
    create_server,
    normalize_benchmark,
    validate_demo_assets,
)
from charon.orchestration.graph_runtime import get_run, start_run


@contextmanager
def _live_server(tmp_path):
    controller = GraphStudioController(tmp_path)
    server = create_server(controller, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield controller, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(server, method, path, *, payload=None, headers=None):
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_port, timeout=3
    )
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        content_type = response.getheader("Content-Type") or ""
        value = (
            json.loads(raw.decode("utf-8"))
            if "application/json" in content_type and raw
            else raw
        )
        return response.status, dict(response.getheaders()), value
    finally:
        connection.close()


def _request_with_host(server, path, host):
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_port, timeout=3
    )
    try:
        connection.putrequest("GET", path, skip_host=True)
        connection.putheader("Host", host)
        connection.endheaders()
        response = connection.getresponse()
        value = json.loads(response.read().decode("utf-8"))
        return response.status, value
    finally:
        connection.close()


def test_assets_validate_and_snapshot_matches_ui_contract(tmp_path):
    validation = validate_demo_assets()
    assert validation["ok"] is True
    assert validation["verified_raw_records"] > 0
    assert set(validation["route_selections"]) == {
        "route_runtime",
        "route_experience",
        "route_evidence",
    }
    assert validation["executable_template"] == {
        "graph_id": "routed-worker",
        "nodes": 2,
        "edges": 1,
        "providers": ["local"],
    }
    controller = GraphStudioController(tmp_path)
    snapshot = controller.snapshot()

    assert set(snapshot) == {
        "definition",
        "run",
        "events",
        "benchmark",
        "control_token",
    }
    assert len(snapshot["control_token"]) >= 32
    definition = snapshot["definition"]
    assert set(definition) == {"graph_id", "name", "nodes", "edges"}
    assert definition["graph_id"] == "software-delivery"
    assert definition["name"] == "Software delivery swarm"
    assert all(
        set(node)
        == {
            "id",
            "label",
            "type",
            "description",
            "position",
            "config",
            "handler",
            "join",
        }
        for node in definition["nodes"]
    )
    by_id = {node["id"]: node for node in definition["nodes"]}
    assert by_id["planner"]["type"] == "agent"
    assert by_id["integration"]["type"] == "tool"
    assert by_id["quality_gate"]["type"] == "evaluator"
    assert by_id["release"]["type"] == "output"
    assert len(by_id["route_runtime"]["config"]["candidates"]) == 3
    assert all(
        set(edge) == {"id", "source", "target", "on", "label", "route"}
        for edge in definition["edges"]
    )
    benchmark = snapshot["benchmark"]
    assert benchmark["recommendation"]["selected_policy_id"] == "balanced"
    assert benchmark["recommendation"]["eligibility"] == {
        "mean_cost_usd_lte": 0.03,
        "p95_latency_ms_lte": 25_000,
    }
    balanced = next(
        policy for policy in benchmark["policies"] if policy["id"] == "balanced"
    )
    assert balanced["scenarios"] == 32
    assert balanced["trials"] == 96
    assert balanced["repetitions_per_scenario"] == 3
    assert balanced["interval_method"] == (
        "scenario_cluster_bootstrap_percentile"
    )
    assert balanced["confidence_level"] == 0.95
    assert benchmark["provenance"]["evaluation_scenario_count"] == 32

    run = snapshot["run"]
    assert set(run) == {
        "run_id",
        "status",
        "active_nodes",
        "node_states",
        "started_at",
        "metrics",
    }
    assert run["status"] == "running"
    assert run["metrics"]["run_steps"] == 2
    assert set(run["active_nodes"]) == {
        "route_runtime",
        "route_experience",
        "route_evidence",
    }
    assert [event["seq"] for event in snapshot["events"]] == sorted(
        event["seq"] for event in snapshot["events"]
    )
    assert all(event["message"] != "Runtime event" for event in snapshot["events"])


def test_asset_validation_rejects_invalid_routing_shares(tmp_path):
    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "routing"
        / "calibration-fixture.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["routing_matrix"][0]["share"] += 0.1
    damaged = tmp_path / "calibration-fixture.json"
    damaged.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(ValueError, match="shares must sum to 1"):
        validate_demo_assets(benchmark_path=damaged)


def test_asset_validation_verifies_raw_corpus_hashes(tmp_path):
    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "routing"
        / "calibration-fixture.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    first = next(iter(fixture["provenance"]["raw_corpora"].values()))
    first["sha256"] = "0" * 64
    damaged = tmp_path / "calibration-fixture.json"
    damaged.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(ValueError, match="raw corpus hash mismatch"):
        validate_demo_assets(benchmark_path=damaged)


def test_benchmark_fields_pareto_and_recommendation_are_data_driven():
    raw = {
        "data_kind": "generated_fixture",
        "seed": 7,
        "description": "fixture",
        "models": [
            {"candidate_id": "small", "model_id": "small"},
            {"candidate_id": "large", "model_id": "large"},
        ],
        "policies": [
            {
                "policy_id": "quality",
                "trials": 10,
                "success_rate": 0.95,
                "success_ci": [0.8, 0.99],
                "mean_score": 0.9,
                "mean_cost_usd": 1.0,
                "p50_latency_ms": 800,
                "p95_latency_ms": 1000,
                "expected_calibration_error": 0.03,
            },
            {
                "policy_id": "economy",
                "trials": 10,
                "success_rate": 0.8,
                "success_ci": [0.6, 0.9],
                "mean_score": 0.75,
                "mean_cost_usd": 0.1,
                "p50_latency_ms": 400,
                "p95_latency_ms": 500,
                "expected_calibration_error": 0.05,
                "recommended": True,
            },
            {
                "policy_id": "dominated",
                "trials": 10,
                "success_rate": 0.7,
                "success_ci": [0.5, 0.8],
                "mean_score": 0.6,
                "mean_cost_usd": 0.2,
                "p50_latency_ms": 500,
                "p95_latency_ms": 600,
                "expected_calibration_error": 0.08,
            },
        ],
        "routing_matrix": [
            {
                "policy_id": "economy",
                "task_class": "code",
                "model_id": "small",
                "share": 0.9,
            }
        ],
    }
    normalized = normalize_benchmark(raw)
    policies = {item["id"]: item for item in normalized["policies"]}
    required = {
        "id",
        "name",
        "samples",
        "pass_rate",
        "quality_mean",
        "quality_ci",
        "cost_mean",
        "latency_p50_ms",
        "latency_p95_ms",
        "calibration_error",
        "pareto",
    }
    assert all(required <= set(item) for item in policies.values())
    assert policies["quality"]["pareto"] is True
    assert policies["economy"]["pareto"] is True
    assert policies["dominated"]["pareto"] is False
    assert policies["economy"]["recommended"] is True
    assert "recommended" not in policies["quality"]
    assert normalized["routing_matrix"][0]["weights"] == {
        "small": 0.9,
        "large": 0.0,
    }


def test_controller_recovers_one_tagged_run_and_stops_only_stale_managed_runs(
    tmp_path,
):
    first = GraphStudioController(tmp_path)
    recovered_id = first.run_id
    stale = start_run(
        tmp_path,
        first.definition,
        metadata={
            "managed_by": "charon.graph_studio.demo",
            "demo_graph_id": first.definition.graph_id,
        },
    )
    unrelated = start_run(
        tmp_path,
        first.definition,
        metadata={"managed_by": "someone-else"},
    )

    restarted = GraphStudioController(tmp_path)
    assert restarted.run_id == recovered_id
    assert restarted.control_token != first.control_token
    assert get_run(tmp_path, recovered_id)["status"] == "running"
    assert get_run(tmp_path, stale["run_id"])["status"] == "stopped"
    assert get_run(tmp_path, unrelated["run_id"])["status"] == "running"


def test_health_static_snapshot_and_traversal_protection(tmp_path):
    with _live_server(tmp_path) as (_, server):
        status, headers, health = _request(server, "GET", "/api/health")
        assert status == 200
        assert health["ok"] is True
        assert headers["Cache-Control"] == "no-store"

        status, _, snapshot = _request(server, "GET", "/api/snapshot")
        assert status == 200
        assert snapshot["definition"]["nodes"]
        assert snapshot["control_token"]

        status, headers, index = _request(server, "GET", "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert b"Graph Studio" in index

        status, headers, script = _request(server, "GET", "/app.js")
        assert status == 200
        assert "javascript" in headers["Content-Type"]
        assert CONTROL_HEADER.encode() in script

        status, _, error = _request(
            server, "GET", "/%2e%2e/examples/routing/calibration-fixture.json"
        )
        assert status == 403
        assert error["error"]["code"] == "invalid_path"

        status, error = _request_with_host(server, "/api/health", "example.test")
        assert status == 403
        assert error["error"]["code"] == "invalid_host"


def test_control_actions_require_same_origin_token_and_json(tmp_path):
    with _live_server(tmp_path) as (_, server):
        _, _, snapshot = _request(server, "GET", "/api/snapshot")
        token = snapshot["control_token"]
        body = {
            "graph_id": snapshot["definition"]["graph_id"],
            "run_id": snapshot["run"]["run_id"],
        }

        status, _, error = _request(
            server, "POST", "/api/demo/tick", payload=body
        )
        assert status == 403
        assert error["error"]["code"] == "invalid_control_token"

        status, _, error = _request(
            server,
            "POST",
            "/api/demo/tick",
            payload=body,
            headers={
                CONTROL_HEADER: token,
                "Content-Type": "text/plain",
            },
        )
        assert status == 415
        assert error["error"]["code"] == "json_required"

        status, _, error = _request(
            server,
            "POST",
            "/api/demo/tick",
            payload=body,
            headers={
                CONTROL_HEADER: token,
                "Origin": "http://example.test",
            },
        )
        assert status == 403
        assert error["error"]["code"] == "invalid_origin"

        origin = f"http://127.0.0.1:{server.server_port}"
        status, _, stepped = _request(
            server,
            "POST",
            "/api/demo/tick",
            payload=body,
            headers={CONTROL_HEADER: token, "Origin": origin},
        )
        assert status == 200
        assert stepped["run"]["metrics"]["run_steps"] == 3

        old_run_id = stepped["run"]["run_id"]
        status, _, reset = _request(
            server,
            "POST",
            "/api/demo/reset",
            payload={
                "graph_id": stepped["definition"]["graph_id"],
                "run_id": old_run_id,
            },
            headers={CONTROL_HEADER: token},
        )
        assert status == 200
        assert reset["run"]["run_id"] != old_run_id
        assert reset["run"]["metrics"]["run_steps"] == 2

        status, _, completed = _request(
            server,
            "POST",
            "/api/demo/run",
            payload={
                "graph_id": reset["definition"]["graph_id"],
                "run_id": reset["run"]["run_id"],
            },
            headers={CONTROL_HEADER: token},
        )
        assert status == 200
        assert completed["run"]["status"] == "completed"
        assert completed["run"]["metrics"]["quality_score"] == pytest.approx(0.93)
        assert completed["events"][-1]["kind"] == "run_completed"


def test_server_refuses_non_loopback_bind(tmp_path):
    controller = GraphStudioController(tmp_path)
    with pytest.raises(ValueError, match="loopback"):
        create_server(controller, host="0.0.0.0", port=0)
