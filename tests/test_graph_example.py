import json
from pathlib import Path

from charon.orchestration.graph_runtime import get_run, start_run, tick_run
from charon.orchestration.graph_schema import GraphDefinition


REPO_ROOT = Path(__file__).resolve().parents[1]


def _delivery_definition() -> GraphDefinition:
    workflow = json.loads(
        (REPO_ROOT / "examples/graph_workflows/software-delivery.json").read_text(
            encoding="utf-8",
        )
    )
    return GraphDefinition.from_dict(workflow)


def test_routed_worker_template_connects_route_to_queue_adapter():
    workflow = json.loads(
        (REPO_ROOT / "examples/graph_workflows/routed-worker.json").read_text(
            encoding="utf-8",
        )
    )
    definition = GraphDefinition.from_dict(workflow)
    nodes = {node.id: node for node in definition.nodes}

    assert nodes["select_model"].handler == "route"
    assert nodes["execute_task"].handler == "queue_agent"
    assert nodes["execute_task"].config["route_from"] == "selected_route"
    assert nodes["select_model"].config["candidates"][0]["provider"] == "local"


def test_software_delivery_example_routes_fans_in_repairs_and_completes(tmp_path):
    definition = _delivery_definition()
    started = start_run(tmp_path, definition)

    for _ in range(32):
        current = get_run(tmp_path, started["run_id"])
        if current["status"] != "running":
            break
        tick_run(tmp_path, started["run_id"])

    final = get_run(tmp_path, started["run_id"])
    states = final["node_states"]

    assert final["status"] == "completed"
    assert final["run_steps"] == 13
    assert states["integration"]["visits"] == 1
    assert states["quality_gate"]["visits"] == 2
    assert states["release"]["status"] == "succeeded"
    assert states["route_runtime"]["output"]["selected_candidate_id"] == "forge-medium"
    assert states["route_experience"]["output"]["selected_candidate_id"] == "oracle-large"
    assert states["route_evidence"]["output"]["selected_candidate_id"] == "ember-small"
    assert states["route_runtime"]["output"]["candidate_source"].endswith(
        "calibration-fixture.json"
    )
    assert states["route_runtime"]["output"]["policy_version"] == "multi-objective-v1"
    assert sum(
        states["route_runtime"]["output"]["policy_weights"].values()
    ) == 1.0
