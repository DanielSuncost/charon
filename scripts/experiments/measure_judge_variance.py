#!/usr/bin/env python3
"""Measure judge score variance on a FIXED working-tree state.

Quantitative and Correctness judges are near-deterministic (they run a command
and parse a number). The AestheticJudge is an LLM grading against a rubric, so
its scores are noisy — and if a judge loop's `min_delta` is below that noise
floor, the loop will "keep" changes that are really just judge noise (hill-climb
noise). This script measures the noise so `min_delta` can be set from data.

Usage:
  PYTHONPATH=apps/core-daemon CHARON_EMBED_BACKEND=local \
    python scripts/experiments/measure_judge_variance.py --n 20 --out results/judge_variance.json

The Aesthetic measurement needs a configured provider (it makes N real LLM
calls). Without one, that judge is reported as skipped.
"""
import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "core-daemon"))

from judge_engine import create_loop, create_judge  # noqa: E402

SAMPLE_CODE = '''\
def summarize(nums):
    # compute some stats over a list of numbers
    t = 0
    for n in nums:
        t = t + n
    avg = t / len(nums)
    mx = nums[0]
    for n in nums:
        if n > mx:
            mx = n
    return {"total": t, "avg": avg, "max": mx}
'''

RUBRIC = (
    "Rate the overall code quality of summarize() on a scale of 1-10, considering "
    "readability, idiomatic Python, edge-case handling (e.g. empty input), and naming. "
    "Return JSON {\"score\": <number>, \"feedback\": \"...\"}."
)


def _stats(scores):
    scores = [s for s in scores if s is not None]
    if not scores:
        return None
    return {
        "n": len(scores),
        "mean": round(statistics.mean(scores), 4),
        "stdev": round(statistics.pstdev(scores), 4) if len(scores) > 1 else 0.0,
        "min": round(min(scores), 4),
        "max": round(max(scores), 4),
        "spread": round(max(scores) - min(scores), 4),
        "scores": [round(s, 4) for s in scores],
    }


def measure(work: Path, config, judge, n: int):
    scores = []
    for _ in range(n):
        v = judge.evaluate(config, work)
        scores.append(None if v.error else v.score)
    return scores


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--out", default="results/judge_variance.json")
    ap.add_argument("--no-aesthetic", action="store_true", help="skip the LLM judge")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="judge_variance_"))
    work = tmp / "project"
    work.mkdir()
    state = tmp / "state"
    state.mkdir()
    (work / "summarize.py").write_text(SAMPLE_CODE)

    report = {"n": args.n, "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "judges": {}}

    # Quantitative — deterministic command output.
    qcfg = create_loop(state, goal="q", project=str(work), agent_id="m",
                       judge_type="quantitative", eval_command="echo 42")
    report["judges"]["quantitative"] = _stats(measure(work, qcfg, create_judge(qcfg), args.n))

    # Correctness — deterministic pass-rate.
    ccfg = create_loop(state, goal="c", project=str(work), agent_id="m",
                       judge_type="correctness", eval_command="echo '8 passed, 2 failed'")
    report["judges"]["correctness"] = _stats(measure(work, ccfg, create_judge(ccfg), args.n))

    # Aesthetic — LLM, the noisy one.
    if args.no_aesthetic:
        report["judges"]["aesthetic"] = {"skipped": "disabled via --no-aesthetic"}
    else:
        try:
            from provider_bridge import create_provider_and_model
            provider, model, ready = create_provider_and_model(state if (state).exists() else Path(".charon_state"),
                                                               )
            if not ready:
                raise RuntimeError("no provider ready")
        except Exception:
            # fall back to the repo's configured state dir
            try:
                from provider_bridge import create_provider_and_model
                provider, model, ready = create_provider_and_model(Path(".charon_state"))
                if not ready:
                    raise RuntimeError("no provider ready")
            except Exception as e2:
                report["judges"]["aesthetic"] = {"skipped": f"no provider: {e2}"}
                provider = None
        if 'provider' in dir() and provider is not None:
            acfg = create_loop(state, goal="improve code quality", project=str(work), agent_id="m",
                               judge_type="aesthetic", rubric=RUBRIC, scope=["summarize.py"])
            ajudge = create_judge(acfg, provider=provider, model=model)
            print(f"measuring aesthetic judge with {type(provider).__name__}/{getattr(model,'model_id',model)} "
                  f"x{args.n} (real LLM calls)...", flush=True)
            report["judges"]["aesthetic"] = _stats(measure(work, acfg, ajudge, args.n))
            report["aesthetic_model"] = getattr(model, "model_id", str(model))

    # Derived min_delta recommendation for stochastic judges.
    aes = report["judges"].get("aesthetic")
    if isinstance(aes, dict) and "stdev" in aes:
        k = 2.0
        report["recommended_min_delta_aesthetic"] = {
            "k": k, "formula": "k * stdev",
            "value": round(k * aes["stdev"], 4),
        }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
