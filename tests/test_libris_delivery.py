import hashlib
import json
import shutil
from pathlib import Path

import pytest

from charon.libris import libris_runtime as lr
from charon.tools import ToolContext
from charon.tools.research_tool import execute_research


def _operation_with_topic(tmp_path, *, status="researching"):
    op = lr.init_operation(
        tmp_path,
        tmp_path,
        prompt="research a topic",
        coordinator_agent_id="AG-coordinator",
    )
    topic = lr.init_topic(
        tmp_path,
        tmp_path,
        op["operation_id"],
        title="A useful topic",
    )
    if status != "researching":
        lr.update_topic_runtime(
            tmp_path,
            tmp_path,
            op["operation_id"],
            topic["slug"],
            status=status,
        )
    return op, topic


def test_finalize_waits_for_active_topics_without_writing_delivery(tmp_path):
    op, _ = _operation_with_topic(tmp_path)

    result = lr.finalize_operation_selection(
        tmp_path,
        tmp_path,
        op["operation_id"],
    )

    assert result["ready"] is False
    assert result["status"] == "waiting"
    assert lr.get_operation_state(
        tmp_path,
        tmp_path,
        op["operation_id"],
    )["status"] == "running"
    assert not (
        lr.operation_dir(tmp_path, tmp_path, op["operation_id"])
        / "delivery"
        / "delivery-bundle.json"
    ).exists()


def test_finalize_rejects_terminal_operation_with_no_checkpoint(tmp_path):
    op, _ = _operation_with_topic(tmp_path, status="judge_failed")

    result = lr.finalize_operation_selection(
        tmp_path,
        tmp_path,
        op["operation_id"],
    )

    assert result["ready"] is False
    assert result["status"] == "incomplete"
    assert "No reviewed topic checkpoint" in result["reason"]
    assert lr.get_operation_state(
        tmp_path,
        tmp_path,
        op["operation_id"],
    )["status"] == "running"


def test_successful_finalize_writes_html_and_validated_manifest(
    tmp_path,
    monkeypatch,
):
    op, topic = _operation_with_topic(tmp_path)
    op_id = op["operation_id"]
    slug = topic["slug"]
    lr.save_report_draft(
        tmp_path,
        tmp_path,
        op_id,
        slug,
        markdown="# Finding\n\nEvidence-backed result.",
    )
    lr.save_checkpoint(
        tmp_path,
        tmp_path,
        op_id,
        slug,
        report_markdown="# Finding\n\nEvidence-backed result.",
        critique_markdown="# Review\n\nLooks sound.",
        summary_markdown="Reviewed.",
        score=8.4,
    )
    lr.update_topic_runtime(
        tmp_path,
        tmp_path,
        op_id,
        slug,
        status="ready_high_confidence",
    )

    result = lr.finalize_operation_selection(tmp_path, tmp_path, op_id)

    assert result["ready"] is True
    manifest = result["delivery_manifest"]
    assert manifest["status"] == "ready"
    assert manifest["topic_count"] == 1
    assert manifest["artifact_count"] >= 6
    assert manifest["primary_artifact"]["path"].endswith("delivery/report.html")
    assert (
        lr.operation_dir(tmp_path, tmp_path, op_id)
        / "delivery"
        / "report.html"
    ).exists()
    assert lr.get_operation_state(tmp_path, tmp_path, op_id)["status"] == "delivered"
    from charon.libris import libris_lifecycle as lifecycle

    machine = lifecycle.load_lifecycle(tmp_path, op_id, entity_type="operation")
    assert machine is not None
    assert machine.state == "completed"
    assert manifest["integrity"]["certificate_valid"] is True
    assert manifest["integrity"]["certificate_bound_to_lifecycle"] is True
    assert Path(manifest["completion_certificate"]["path"]).is_file()

    revision = machine.revision
    repeated = lr.finalize_operation_selection(tmp_path, tmp_path, op_id)
    assert repeated["ready"] is True
    assert repeated["idempotent"] is True
    assert lifecycle.load_lifecycle(
        tmp_path, op_id, entity_type="operation"
    ).revision == revision
    assert lr.get_libris_swarm_state(
        tmp_path,
        tmp_path,
        op_id,
    )["delivery_manifest"]["ready"] is True

    monkeypatch.chdir(tmp_path.parent)
    relative_manifest = lr.get_delivery_manifest(
        Path(tmp_path.name),
        tmp_path,
        op_id,
    )
    assert relative_manifest["ready"] is True
    assert Path(relative_manifest["primary_artifact"]["path"]).is_absolute()
    assert all(
        Path(artifact["path"]).is_absolute()
        for artifact in relative_manifest["artifacts"]
    )

    topic_report = next(
        artifact
        for artifact in manifest["artifacts"]
        if artifact["kind"] == "topic_report"
    )
    Path(topic_report["path"]).unlink()
    damaged = lr.get_delivery_manifest(tmp_path, tmp_path, op_id)
    assert damaged["ready"] is False
    assert damaged["status"] == "incomplete"


def test_generic_status_writer_cannot_deliver_zero_topics(tmp_path):
    op = lr.init_operation(tmp_path, tmp_path, prompt="empty")

    with pytest.raises(Exception, match="completion certificate"):
        lr.set_operation_status(tmp_path, tmp_path, op["operation_id"], "delivered")

    state = lr.get_operation_state(tmp_path, tmp_path, op["operation_id"])
    assert state["status"] == "running"
    assert not (
        lr.operation_dir(tmp_path, tmp_path, op["operation_id"])
        / "delivery"
        / "completion-certificate.json"
    ).exists()


def test_forged_minimal_certificate_cannot_complete_operation(tmp_path):
    from charon.libris import libris_lifecycle as lifecycle
    from charon.orchestration.fsm import TransitionRejected

    op = lr.init_operation(tmp_path, tmp_path, prompt="forged")
    op_id = op["operation_id"]
    lr.set_operation_status(tmp_path, tmp_path, op_id, "assembling_delivery")
    lr.set_operation_status(tmp_path, tmp_path, op_id, "verifying_delivery")
    op_dir = lr.operation_dir(tmp_path, tmp_path, op_id)
    path = op_dir / "delivery" / "completion-certificate.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    forged = {
        "schema_version": 1,
        "operation_id": op_id,
        "issued_at": "2026-01-01T00:00:00+00:00",
        "topic_count": 1,
        "topics": [],
        "checks": {},
        "artifacts": [],
    }
    forged["certificate_sha256"] = hashlib.sha256(
        json.dumps(
            forged,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    path.write_text(json.dumps(forged), encoding="utf-8")

    with pytest.raises(TransitionRejected, match="completion checks"):
        lifecycle.complete_operation(
            tmp_path,
            operation_id=op_id,
            operation_dir=op_dir,
            current_projected_status="verifying_delivery",
            certificate={
                **forged,
                "valid": True,
                "path": str(path.resolve()),
            },
        )

    assert lifecycle.load_lifecycle(
        tmp_path, op_id, entity_type="operation"
    ).state == "verifying"


def test_finalization_resumes_from_verifying_after_terminal_commit_crash(
    tmp_path, monkeypatch
):
    from charon.libris import libris_lifecycle as lifecycle

    op, topic = _operation_with_topic(tmp_path)
    op_id = op["operation_id"]
    slug = topic["slug"]
    lr.save_checkpoint(
        tmp_path,
        tmp_path,
        op_id,
        slug,
        report_markdown="# Result\n\nSupported finding.",
        critique_markdown="# Review\n\nSound.",
        summary_markdown="Reviewed.",
        score=8.0,
    )
    lr.update_topic_runtime(
        tmp_path,
        tmp_path,
        op_id,
        slug,
        status="ready_high_confidence",
    )
    real_complete = lifecycle.complete_operation

    def crash_before_commit(*args, **kwargs):
        raise RuntimeError("simulated crash before terminal commit")

    monkeypatch.setattr(lifecycle, "complete_operation", crash_before_commit)
    with pytest.raises(RuntimeError, match="simulated crash"):
        lr.finalize_operation_selection(tmp_path, tmp_path, op_id)
    assert lr.get_operation_state(tmp_path, tmp_path, op_id)["status"] == "verifying_delivery"

    monkeypatch.setattr(lifecycle, "complete_operation", real_complete)
    resumed = lr.finalize_operation_selection(tmp_path, tmp_path, op_id)
    assert resumed["ready"] is True
    assert lr.get_operation_state(tmp_path, tmp_path, op_id)["status"] == "delivered"


def test_finalization_reconciles_outputs_after_terminal_projection_crash(
    tmp_path,
    monkeypatch,
):
    from charon.libris import libris_lifecycle as lifecycle

    op, topic = _operation_with_topic(tmp_path)
    op_id = op["operation_id"]
    lr.save_checkpoint(
        tmp_path,
        tmp_path,
        op_id,
        topic["slug"],
        report_markdown="# Result\n\nSupported finding.",
        critique_markdown="# Review\n\nSound.",
        summary_markdown="Reviewed.",
        score=8.0,
    )
    lr.update_topic_runtime(
        tmp_path,
        tmp_path,
        op_id,
        topic["slug"],
        status="ready_high_confidence",
    )
    real_mutate = lifecycle.mutate_projection
    crashed = False

    def crash_after_terminal_commit(path, mutator):
        nonlocal crashed
        machine = lifecycle.load_lifecycle(
            tmp_path,
            op_id,
            entity_type="operation",
        )
        if not crashed and machine is not None and machine.state == "completed":
            crashed = True
            raise RuntimeError("simulated projection crash")
        return real_mutate(path, mutator)

    monkeypatch.setattr(lifecycle, "mutate_projection", crash_after_terminal_commit)
    with pytest.raises(RuntimeError, match="simulated projection crash"):
        lr.finalize_operation_selection(tmp_path, tmp_path, op_id)
    machine = lifecycle.load_lifecycle(tmp_path, op_id, entity_type="operation")
    assert machine is not None and machine.state == "completed"
    manifest_path = (
        lr.operation_dir(tmp_path, tmp_path, op_id)
        / "delivery"
        / "manifest.json"
    )
    assert not manifest_path.exists()

    monkeypatch.setattr(lifecycle, "mutate_projection", real_mutate)
    resumed = lr.finalize_operation_selection(tmp_path, tmp_path, op_id)

    assert resumed["ready"] is True
    assert manifest_path.is_file()
    persisted_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert persisted_manifest == resumed["delivery_manifest"]
    projection = json.loads(
        (
            lr.operation_dir(tmp_path, tmp_path, op_id)
            / "operation.json"
        ).read_text(encoding="utf-8")
    )
    assert projection["status"] == "delivered"
    assert projection["completion_certificate_path"] == (
        persisted_manifest["completion_certificate"]["path"]
    )
    assert projection["completion_certificate_sha256"] == (
        persisted_manifest["completion_certificate"]["certificate_sha256"]
    )
    events = lr._iter_jsonl(
        lr.operation_dir(tmp_path, tmp_path, op_id) / "events.jsonl"
    )
    transition_id = str(machine.data.get("last_transition_event_id") or "")
    assert transition_id
    assert sum(event.get("event_id") == transition_id for event in events) == 1


def test_valid_legacy_delivery_backfills_certificate_without_reordering_operation(tmp_path):
    from charon.libris import libris_lifecycle as lifecycle

    op, topic = _operation_with_topic(tmp_path)
    op_id = op["operation_id"]
    lr.save_checkpoint(
        tmp_path,
        tmp_path,
        op_id,
        topic["slug"],
        report_markdown="# Result\n\nSupported finding.",
        critique_markdown="# Review\n\nSound.",
        summary_markdown="Reviewed.",
        score=8.0,
    )
    lr.update_topic_runtime(
        tmp_path,
        tmp_path,
        op_id,
        topic["slug"],
        status="ready_high_confidence",
    )
    assert lr.finalize_operation_selection(tmp_path, tmp_path, op_id)["ready"] is True
    op_path = lr.operation_dir(tmp_path, tmp_path, op_id) / "operation.json"
    before = json.loads(op_path.read_text(encoding="utf-8"))
    machine_path = lifecycle.lifecycle_path(tmp_path, op_id, entity_type="operation")
    shutil.rmtree(machine_path.parent)
    certificate_path = (
        lr.operation_dir(tmp_path, tmp_path, op_id)
        / "delivery"
        / "completion-certificate.json"
    )
    certificate_path.unlink()
    manifest_path = certificate_path.with_name("manifest.json")
    stale_manifest = {
        "operation_id": op_id,
        "status": "ready",
        "ready": True,
        "artifact_count": 0,
    }
    manifest_path.write_text(json.dumps(stale_manifest), encoding="utf-8")
    legacy = json.loads(op_path.read_text(encoding="utf-8"))
    for key in (
        "lifecycle_state",
        "lifecycle_revision",
        "lifecycle_event_seq",
        "lifecycle_updated_at",
        "completion_certificate_path",
        "completion_certificate_sha256",
    ):
        legacy.pop(key, None)
    op_path.write_text(json.dumps(legacy), encoding="utf-8")

    manifest = lr.get_delivery_manifest(tmp_path, tmp_path, op_id)

    after = json.loads(op_path.read_text(encoding="utf-8"))
    persisted_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["ready"] is True
    assert manifest["integrity"]["certificate_bound_to_lifecycle"] is True
    assert persisted_manifest == manifest
    assert persisted_manifest != stale_manifest
    assert after["completion_certificate_path"] == (
        manifest["completion_certificate"]["path"]
    )
    assert after["completion_certificate_sha256"] == (
        manifest["completion_certificate"]["certificate_sha256"]
    )
    assert after["updated_at"] == before["updated_at"]
    machine = lifecycle.load_lifecycle(
        tmp_path, op_id, entity_type="operation"
    )
    assert machine.state == "completed"

    projection_bytes = op_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()
    repeated = lr.get_delivery_manifest(tmp_path, tmp_path, op_id)

    assert repeated == manifest
    assert op_path.read_bytes() == projection_bytes
    assert manifest_path.read_bytes() == manifest_bytes
    assert lifecycle.load_lifecycle(
        tmp_path, op_id, entity_type="operation"
    ).revision == machine.revision


def test_finalize_rejects_checkpoint_with_missing_report_file(tmp_path):
    op, topic = _operation_with_topic(tmp_path)
    op_id = op["operation_id"]
    slug = topic["slug"]
    checkpoint = lr.save_checkpoint(
        tmp_path,
        tmp_path,
        op_id,
        slug,
        report_markdown="# Finding\n\nEvidence-backed result.",
        critique_markdown="# Review\n\nLooks sound.",
        summary_markdown="Reviewed.",
        score=8.4,
    )
    lr.update_topic_runtime(
        tmp_path,
        tmp_path,
        op_id,
        slug,
        status="ready_high_confidence",
    )
    Path(checkpoint["report_path"]).unlink()

    result = lr.finalize_operation_selection(tmp_path, tmp_path, op_id)

    assert result["ready"] is False
    assert result["status"] == "incomplete"
    assert "none could be copied" in result["reason"]
    assert lr.get_operation_state(tmp_path, tmp_path, op_id)["status"] == "running"
    assert not (
        lr.operation_dir(tmp_path, tmp_path, op_id)
        / "delivery"
        / "report.html"
    ).exists()


def test_background_coordinator_cannot_take_controller_actions(tmp_path):
    ctx = ToolContext(
        project_root=tmp_path,
        state_dir=tmp_path,
        agent_id="AG-coordinator",
        operation_id="rop_test",
        operation_domain="research",
        operation_role="coordinator",
        runtime_role="background_agent",
    )

    for action in ("init_topic", "finalize_delivery", "finalize_operation_selection"):
        result = execute_research(
            {
                "action": action,
                "operation_id": "rop_test",
                "topic_slug": "topic",
                "checkpoint_id": "ckp_001",
                "title": "Should not be created",
            },
            ctx,
        )
        assert result.is_error is True
        assert "controller" in result.content.lower()


def test_topic_state_response_exposes_draft_and_checkpoint_paths(tmp_path):
    op, topic = _operation_with_topic(tmp_path)
    op_id = op["operation_id"]
    slug = topic["slug"]
    lr.save_report_draft(
        tmp_path,
        tmp_path,
        op_id,
        slug,
        markdown="# Draft",
    )
    lr.save_checkpoint(
        tmp_path,
        tmp_path,
        op_id,
        slug,
        report_markdown="# Draft",
        critique_markdown="# Critique",
        summary_markdown="Summary",
        score=7.0,
    )
    ctx = ToolContext(project_root=tmp_path, state_dir=tmp_path)

    result = execute_research(
        {
            "action": "get_topic_state",
            "operation_id": op_id,
            "topic_slug": slug,
        },
        ctx,
    )

    assert result.is_error is False
    assert "Draft report:" in result.content
    assert "draft-report.md" in result.content
    assert "Latest checkpoint report:" in result.content
    assert "-report.md" in result.content
