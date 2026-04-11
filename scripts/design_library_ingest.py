#!/usr/bin/env python3
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "design-library"
SOURCES_PATH = LIB / "sources" / "sources.json"
EXAMPLES_INBOX = LIB / "examples" / "inbox"
INDEX_BY_SOURCE = LIB / "indexes" / "examples-by-source.json"


def slugify(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:48] or "example"


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def ensure_source(source_id: str):
    sources = load_json(SOURCES_PATH, [])
    for src in sources:
        if src.get("source_id") == source_id:
            return src
    raise SystemExit(f"Unknown source_id: {source_id}. Add it to {SOURCES_PATH} first.")


def infer_title(raw_text: str, fallback: str) -> str:
    raw_text = (raw_text or "").strip()
    if raw_text:
        return raw_text[:120]
    return fallback


def next_example_id(source_id: str, captured_at: str, raw_text: str) -> str:
    date_part = captured_at[:10].replace("-", "_")
    source_part = source_id.replace("src_", "")
    slug = slugify(raw_text)
    prefix = f"ex_{source_part}_{date_part}"
    existing = sorted(EXAMPLES_INBOX.glob(f"{prefix}_*.json"))
    seq = len(existing) + 1
    return f"{prefix}_{seq:03d}"


def ingest(args):
    ensure_source(args.source_id)
    captured_at = args.captured_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    title = infer_title(args.title or args.raw_text, "Untitled example")
    example_id = next_example_id(args.source_id, captured_at, args.raw_text or title)
    record = {
        "example_id": example_id,
        "source_id": args.source_id,
        "title": title,
        "source_url": args.source_url,
        "canonical_url": args.canonical_url or args.source_url,
        "creator": args.creator,
        "product_or_brand": args.product_or_brand,
        "captured_at": captured_at,
        "surface_type": args.surface_type,
        "platform": args.platform,
        "image_paths": args.image_paths or [],
        "video_paths": args.video_paths or [],
        "thumbnail_path": args.thumbnail_path,
        "raw_text": args.raw_text,
        "summary": args.summary,
        "why_notable": args.why_notable,
        "quality_score": args.quality_score,
        "training_candidate": args.training_candidate,
        "status": args.status,
        "visual_style_tags": args.visual_style_tags or [],
        "component_tags": args.component_tags or [],
        "interaction_tags": args.interaction_tags or []
    }
    path = EXAMPLES_INBOX / f"{example_id}.json"
    save_json(path, record)

    by_source = load_json(INDEX_BY_SOURCE, {})
    by_source.setdefault(args.source_id, [])
    by_source[args.source_id].append(example_id)
    save_json(INDEX_BY_SOURCE, by_source)

    print(json.dumps({"ok": True, "example_id": example_id, "path": str(path.relative_to(ROOT))}, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Ingest a design example into the design library inbox.")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--canonical-url")
    parser.add_argument("--title")
    parser.add_argument("--creator")
    parser.add_argument("--product-or-brand")
    parser.add_argument("--captured-at")
    parser.add_argument("--surface-type", default="unknown")
    parser.add_argument("--platform", default="unknown")
    parser.add_argument("--thumbnail-path")
    parser.add_argument("--raw-text", default="")
    parser.add_argument("--summary", default="")
    parser.add_argument("--why-notable", default="")
    parser.add_argument("--quality-score", type=float, default=0.0)
    parser.add_argument("--training-candidate", action="store_true")
    parser.add_argument("--status", default="inbox")
    parser.add_argument("--image-path", dest="image_paths", action="append")
    parser.add_argument("--video-path", dest="video_paths", action="append")
    parser.add_argument("--visual-style-tag", dest="visual_style_tags", action="append")
    parser.add_argument("--component-tag", dest="component_tags", action="append")
    parser.add_argument("--interaction-tag", dest="interaction_tags", action="append")
    args = parser.parse_args()
    ingest(args)


if __name__ == "__main__":
    main()
