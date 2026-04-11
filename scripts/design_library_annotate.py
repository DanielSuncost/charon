#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "design-library"
INDEX_BY_SOURCE = LIB / "indexes" / "examples-by-source.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def find_example(example_id: str):
    for subdir in ["inbox", "annotated", "approved", "rejected"]:
        path = LIB / "examples" / subdir / f"{example_id}.json"
        if path.exists():
            return path, load_json(path, {})
    raise SystemExit(f"Example not found: {example_id}")


def parse_csv(value: str | None):
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def move_example(path: Path, example_id: str, status: str) -> Path:
    target_dir = LIB / "examples" / ("annotated" if status == "annotated" else path.parent.name)
    if status == "approved":
        target_dir = LIB / "examples" / "approved"
    elif status == "rejected":
        target_dir = LIB / "examples" / "rejected"
    elif status in {"inbox", "pattern_extracted", "shortlisted", "duplicate"}:
        target_dir = path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{example_id}.json"
    if path != target:
        path.rename(target)
    return target


def annotate(args):
    path, record = find_example(args.example_id)

    updates = {
        "surface_type": args.surface_type,
        "platform": args.platform,
        "summary": args.summary,
        "why_notable": args.why_notable,
        "quality_score": args.quality_score,
        "training_candidate": args.training_candidate,
        "status": args.status,
        "visual_style_tags": parse_csv(args.visual_style_tags),
        "interaction_tags": parse_csv(args.interaction_tags),
        "component_tags": parse_csv(args.component_tags),
        "layout_tags": parse_csv(args.layout_tags),
        "ux_tags": parse_csv(args.ux_tags),
        "motion_tags": parse_csv(args.motion_tags),
        "design_language_tags": parse_csv(args.design_language_tags),
        "strengths": parse_csv(args.strengths),
        "weaknesses": parse_csv(args.weaknesses),
        "annotation_confidence": args.annotation_confidence,
        "annotator": args.annotator,
        "annotated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }

    for key, value in updates.items():
        if value is not None:
            record[key] = value

    save_json(path, record)
    new_path = move_example(path, args.example_id, record.get("status", "inbox"))

    print(json.dumps({
        "ok": True,
        "example_id": args.example_id,
        "path": str(new_path.relative_to(ROOT)),
        "status": record.get("status")
    }, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Annotate a design library example.")
    parser.add_argument("--example-id", required=True)
    parser.add_argument("--surface-type")
    parser.add_argument("--platform")
    parser.add_argument("--summary")
    parser.add_argument("--why-notable")
    parser.add_argument("--quality-score", type=float)
    parser.add_argument("--training-candidate", action="store_true")
    parser.add_argument("--status")
    parser.add_argument("--visual-style-tags")
    parser.add_argument("--interaction-tags")
    parser.add_argument("--component-tags")
    parser.add_argument("--layout-tags")
    parser.add_argument("--ux-tags")
    parser.add_argument("--motion-tags")
    parser.add_argument("--design-language-tags")
    parser.add_argument("--strengths")
    parser.add_argument("--weaknesses")
    parser.add_argument("--annotation-confidence", type=float)
    parser.add_argument("--annotator")
    args = parser.parse_args()
    annotate(args)


if __name__ == "__main__":
    main()
