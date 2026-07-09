#!/usr/bin/env python3
"""Run LongMemEval official evaluation using OpenRouter as the judge.

Reimplements evaluate_qa.py logic but routes through OpenRouter
so we don't need a separate OpenAI key.

Usage:
    OPENROUTER_API_KEY=sk-or-... python scripts/experiments/eval_longmemeval.py \
        results/longmemeval/hyp_openai_gpt-4o_*.jsonl
"""
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
DATA_FILE = ROOT / "data" / "longmemeval" / "longmemeval_s_cleaned.json"

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
JUDGE_MODEL = "openai/gpt-4o"


def get_eval_prompt(task, question, answer, response, abstention=False):
    """Exact prompts from LongMemEval's evaluate_qa.py."""
    if not abstention:
        if task in ['single-session-user', 'single-session-assistant', 'multi-session']:
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        elif task == 'temporal-reasoning':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        elif task == 'knowledge-update':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        elif task == 'single-session-preference':
            template = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        else:
            raise NotImplementedError(f"Unknown task: {task}")
    else:
        template = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."
    return template.format(question, answer, response)


def judge(prompt: str, retries: int = 3) -> bool:
    """Call GPT-4o via OpenRouter to judge a response."""
    for attempt in range(retries):
        try:
            resp = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json={
                    "model": JUDGE_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 10,
                },
                timeout=30,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip().lower()
            return "yes" in text
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"  Judge error after {retries} retries: {e}")
                return False


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/experiments/eval_longmemeval.py <hypothesis_file>")
        sys.exit(1)

    hyp_file = Path(sys.argv[1])
    if not hyp_file.exists():
        print(f"File not found: {hyp_file}")
        sys.exit(1)

    if not OPENROUTER_API_KEY:
        print("Set OPENROUTER_API_KEY environment variable")
        sys.exit(1)

    data = json.load(open(DATA_FILE))
    qid_to_item = {d["question_id"]: d for d in data}

    hyps = [json.loads(line) for line in open(hyp_file)]
    print(f"Evaluating {len(hyps)} hypotheses with {JUDGE_MODEL}")
    print()

    type_scores = defaultdict(list)
    results = []

    for i, h in enumerate(hyps):
        qid = h["question_id"]
        hypothesis = str(h["hypothesis"])
        item = qid_to_item.get(qid)
        if not item:
            continue

        qtype = item["question_type"]
        question = item["question"]
        answer = str(item["answer"])
        is_abstention = "_abs" in qid

        prompt = get_eval_prompt(qtype, question, answer, hypothesis, abstention=is_abstention)
        label = judge(prompt)

        type_scores[qtype].append(1 if label else 0)
        results.append({"question_id": qid, "label": label, "type": qtype})

        if (i + 1) % 25 == 0 or i == len(hyps) - 1:
            total_correct = sum(s for scores in type_scores.values() for s in scores)
            total_done = sum(len(scores) for scores in type_scores.values())
            print(f"  [{i+1}/{len(hyps)}] Running accuracy: {total_correct}/{total_done} = {total_correct/total_done*100:.1f}%")

    # Final results
    print()
    print("=" * 60)
    print("OFFICIAL LONGMEMEVAL_S RESULTS (GPT-4o judge)")
    print("=" * 60)

    total_correct = 0
    total_all = 0
    for qtype in sorted(type_scores.keys()):
        scores = type_scores[qtype]
        correct = sum(scores)
        total = len(scores)
        total_correct += correct
        total_all += total
        print(f"  {qtype:35s} {correct:3d}/{total:3d} = {correct/total*100:5.1f}%")

    print(f"  {'OVERALL':35s} {total_correct:3d}/{total_all:3d} = {total_correct/total_all*100:5.1f}%")
    print()
    print("  Supermemory benchmark:  81.6%")
    print(f"  Charon (this run):      {total_correct/total_all*100:.1f}%")

    # Save detailed results
    out_file = hyp_file.with_suffix(".eval.json")
    out_file.write_text(json.dumps({
        "overall": total_correct / total_all,
        "by_type": {k: sum(v)/len(v) for k, v in type_scores.items()},
        "details": results,
    }, indent=2))
    print(f"\n  Saved to {out_file}")


if __name__ == "__main__":
    main()
