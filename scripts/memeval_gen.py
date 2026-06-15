#!/usr/bin/env python3
"""Synthetic multi-session agentic-memory trajectory generator (with ground truth).

Emits a dataset of timestamped multi-session conversations plus questions, each
with gold supporting session/turn ids and a checkable answer. Difficulty is
*controllable* along the axes an agentic-memory benchmark cares about:

  - distractors      : irrelevant-but-plausible facts that pollute retrieval.
  - knowledge updates: an attribute's value changes across sessions (tests
                       latest-value, not first-seen).
  - cross-session joins: an answer that requires combining facts from >= 2
                       different sessions (multi-session reasoning).
  - temporal ordering: which of two things was mentioned first.

The point is not scale — it's *authorship + ground truth + control*, so a harness
can measure per-type retrieval recall@k and answer-correctness, and isolate which
difficulty axis breaks memory. Deterministic given --seed. Self-validating: every
question must be answerable from its declared gold turns.

  python scripts/memeval_gen.py --difficulty medium --seed 0 --out results/memeval/medium_s0.json
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

# ── content pools (kept small + legible; values are clean single tokens so
#    answers are exact-checkable) ─────────────────────────────────────────────
FIRST_NAMES = ["Maria", "David", "Priya", "Kenji", "Lena", "Omar", "Sofia", "Tariq",
               "Nina", "Hugo", "Aisha", "Diego", "Yuki", "Clara", "Mateo", "Iris"]
COMPANIES = ["Northwind", "Acme", "Globex", "Initech", "Umbrella", "Hooli", "Soylent",
             "Stark", "Wayne", "Cyberdyne", "Tyrell", "Pied Piper"]
CITIES = ["Lisbon", "Osaka", "Toronto", "Nairobi", "Bogota", "Helsinki", "Manila",
          "Lyon", "Austin", "Prague", "Accra", "Quito"]
INDUSTRIES = ["logistics", "biotech", "gaming", "aerospace", "fintech", "agriculture"]
PETS = ["a beagle", "a tabby cat", "a parrot", "two goldfish", "a corgi", "a python"]
HOBBIES = ["bouldering", "pottery", "freediving", "birdwatching", "chess", "baking"]

# natural-ish surface templates per (entity, attribute) → text + answer value
TEMPLATES = {
    ("person", "employer"): "By the way, {name} just started a new job at {value}.",
    ("person", "city"): "{name} moved to {value} last month — really settling in.",
    ("person", "pet"): "Funny story, {name} adopted {value} over the weekend.",
    ("person", "hobby"): "{name} has gotten really into {value} lately.",
    ("company", "hq_city"): "I read that {name} is headquartered in {value}.",
    ("company", "industry"): "Turns out {name} is mostly a {value} company.",
}
DISTRACTOR_TEMPLATES = [
    "The weather has been unpredictable all week.",
    "I finally finished that book I was telling you about.",
    "We might reschedule the team sync to Thursday.",
    "The new coffee place downtown is surprisingly good.",
    "Traffic on the bridge was terrible this morning.",
    "I think the package is arriving tomorrow.",
]


def _ts(day: int) -> str:
    # deterministic ISO-ish timestamp; day offset from a fixed epoch (no wall clock)
    base_y, base_m = 2025, 1
    d = day % 28 + 1
    m = base_m + (day // 28) % 12
    y = base_y + (day // 28) // 12
    return f"{y:04d}-{m:02d}-{d:02d}T10:00:00"


class Gen:
    def __init__(self, rng: random.Random, n_sessions: int, turns_per_session: int,
                 distractor_ratio: float, n_updates: int, n_joins: int, n_temporal: int):
        self.rng = rng
        self.n_sessions = n_sessions
        self.turns_per_session = turns_per_session
        self.distractor_ratio = distractor_ratio
        self.n_updates = n_updates
        self.n_joins = n_joins
        self.n_temporal = n_temporal
        self.sessions: list[dict] = [
            {"session_id": f"s{i}", "timestamp": _ts(i * 3), "turns": []}
            for i in range(n_sessions)
        ]
        self.questions: list[dict] = []
        self._people = self.rng.sample(FIRST_NAMES, k=min(len(FIRST_NAMES), max(6, n_sessions)))
        self._companies = self.rng.sample(COMPANIES, k=min(len(COMPANIES), 8))
        self._turn_counters = {s["session_id"]: 0 for s in self.sessions}

    def _add_fact_turn(self, sid: str, etype: str, name: str, attr: str, value: str) -> str:
        s = next(s for s in self.sessions if s["session_id"] == sid)
        tid = f"{sid}:t{self._turn_counters[sid]}"
        self._turn_counters[sid] += 1
        text = TEMPLATES[(etype, attr)].format(name=name, value=value)
        s["turns"].append({"turn_id": tid, "speaker": "user", "text": text,
                           "fact": {"etype": etype, "name": name, "attr": attr, "value": value}})
        return tid

    def _add_distractor(self, sid: str) -> None:
        s = next(s for s in self.sessions if s["session_id"] == sid)
        tid = f"{sid}:t{self._turn_counters[sid]}"
        self._turn_counters[sid] += 1
        s["turns"].append({"turn_id": tid, "speaker": "user",
                           "text": self.rng.choice(DISTRACTOR_TEMPLATES), "fact": None})

    def _value_for(self, attr: str) -> str:
        return self.rng.choice({"employer": self._companies, "city": CITIES, "pet": PETS,
                                "hobby": HOBBIES, "hq_city": CITIES,
                                "industry": INDUSTRIES}[attr])

    # ── question builders ────────────────────────────────────────────────────
    def _q_single(self) -> None:
        name = self.rng.choice(self._people)
        attr = self.rng.choice(["pet", "hobby"])
        value = self._value_for(attr)
        sid = self.rng.choice([s["session_id"] for s in self.sessions])
        tid = self._add_fact_turn(sid, "person", name, attr, value)
        qtext = {"pet": f"What pet did {name} adopt?",
                 "hobby": f"What hobby has {name} gotten into?"}[attr]
        self.questions.append({"qid": f"q{len(self.questions)}", "type": "single_session",
                               "question": qtext, "answer": value,
                               "gold_session_ids": [sid], "gold_turn_ids": [tid]})

    def _q_update(self) -> None:
        # same attribute stated in two sessions (earlier -> later); ask current value.
        name = self.rng.choice(self._people)
        sids = sorted(self.rng.sample([s["session_id"] for s in self.sessions], k=2),
                      key=lambda x: int(x[1:]))
        old, new = self._value_for("city"), self._value_for("city")
        while new == old:
            new = self._value_for("city")
        self._add_fact_turn(sids[0], "person", name, "city", old)
        tid_new = self._add_fact_turn(sids[1], "person", name, "city", new)
        self.questions.append({"qid": f"q{len(self.questions)}", "type": "knowledge_update",
                               "question": f"Where does {name} currently live?", "answer": new,
                               "gold_session_ids": [sids[1]], "gold_turn_ids": [tid_new],
                               "distractor_answer": old})

    def _q_join(self) -> None:
        # person -> employer (session A); company -> hq_city (session B).
        # "Where is {person}'s employer headquartered?" needs BOTH sessions.
        name = self.rng.choice(self._people)
        company = self.rng.choice(self._companies)
        hq = self._value_for("hq_city")
        sids = self.rng.sample([s["session_id"] for s in self.sessions], k=2)
        t1 = self._add_fact_turn(sids[0], "person", name, "employer", company)
        t2 = self._add_fact_turn(sids[1], "company", company, "hq_city", hq)
        self.questions.append({"qid": f"q{len(self.questions)}", "type": "multi_session_join",
                               "question": f"In which city is {name}'s employer headquartered?",
                               "answer": hq, "gold_session_ids": sids, "gold_turn_ids": [t1, t2],
                               "join_via": company})

    def _q_temporal(self) -> None:
        a, b = self.rng.sample(self._people, k=2)
        sids = sorted(self.rng.sample([s["session_id"] for s in self.sessions], k=2),
                      key=lambda x: int(x[1:]))
        ta = self._add_fact_turn(sids[0], "person", a, "hobby", self._value_for("hobby"))
        tb = self._add_fact_turn(sids[1], "person", b, "hobby", self._value_for("hobby"))
        self.questions.append({"qid": f"q{len(self.questions)}", "type": "temporal",
                               "question": f"Whose news did the user mention first, {a} or {b}?",
                               "answer": a, "gold_session_ids": [sids[0], sids[1]],
                               "gold_turn_ids": [ta, tb]})

    def build(self) -> dict:
        for _ in range(self.n_updates):
            self._q_update()
        for _ in range(self.n_joins):
            self._q_join()
        for _ in range(self.n_temporal):
            self._q_temporal()
        # fill remaining capacity with single-session facts (each adds a question)
        planted = sum(len(s["turns"]) for s in self.sessions)
        capacity = self.n_sessions * self.turns_per_session
        target_facts = int(capacity * (1 - self.distractor_ratio))
        while planted < target_facts:
            self._q_single()
            planted += 1
        # distractors fill the rest
        for s in self.sessions:
            while len(s["turns"]) < self.turns_per_session:
                self._add_distractor(s["session_id"])
            self.rng.shuffle(s["turns"])
        return {
            "meta": {"n_sessions": self.n_sessions, "turns_per_session": self.turns_per_session,
                     "distractor_ratio": self.distractor_ratio, "n_updates": self.n_updates,
                     "n_joins": self.n_joins, "n_temporal": self.n_temporal,
                     "n_questions": len(self.questions),
                     "type_counts": _type_counts(self.questions)},
            "sessions": self.sessions, "questions": self.questions,
        }


def _type_counts(questions: list[dict]) -> dict:
    c: dict[str, int] = {}
    for q in questions:
        c[q["type"]] = c.get(q["type"], 0) + 1
    return c


def validate(dataset: dict) -> list[str]:
    """Self-check: every question's answer must be derivable from its gold turns."""
    turns = {t["turn_id"]: t for s in dataset["sessions"] for t in s["turns"]}
    errors = []
    for q in dataset["questions"]:
        gold = [turns.get(tid) for tid in q["gold_turn_ids"]]
        if any(g is None for g in gold):
            errors.append(f"{q['qid']}: missing gold turn"); continue
        facts = [g["fact"] for g in gold if g["fact"]]
        if q["type"] in ("single_session", "knowledge_update"):
            if not any(f["value"] == q["answer"] for f in facts):
                errors.append(f"{q['qid']}: answer not in gold turn")
        elif q["type"] == "multi_session_join":
            via = q["join_via"]
            has_link = any(f["attr"] == "employer" and f["value"] == via for f in facts)
            has_hq = any(f["attr"] == "hq_city" and f["value"] == q["answer"] for f in facts)
            if not (has_link and has_hq):
                errors.append(f"{q['qid']}: join chain incomplete")
            if len(set(q["gold_session_ids"])) < 2:
                errors.append(f"{q['qid']}: join not cross-session")
        elif q["type"] == "temporal":
            if int(q["gold_session_ids"][0][1:]) >= int(q["gold_session_ids"][1][1:]):
                errors.append(f"{q['qid']}: temporal order wrong")
    return errors


PRESETS = {
    "easy":   dict(n_sessions=4, turns_per_session=5, distractor_ratio=0.2, n_updates=1, n_joins=1, n_temporal=1),
    "medium": dict(n_sessions=8, turns_per_session=8, distractor_ratio=0.4, n_updates=3, n_joins=3, n_temporal=2),
    "hard":   dict(n_sessions=15, turns_per_session=12, distractor_ratio=0.6, n_updates=6, n_joins=6, n_temporal=4),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--difficulty", choices=list(PRESETS), default="medium")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="")
    # individual overrides
    for k in ("n_sessions", "turns_per_session", "n_updates", "n_joins", "n_temporal"):
        ap.add_argument(f"--{k}", type=int, default=None)
    ap.add_argument("--distractor_ratio", type=float, default=None)
    args = ap.parse_args()

    cfg = dict(PRESETS[args.difficulty])
    for k in ("n_sessions", "turns_per_session", "n_updates", "n_joins", "n_temporal",
              "distractor_ratio"):
        if getattr(args, k) is not None:
            cfg[k] = getattr(args, k)

    rng = random.Random(args.seed)
    dataset = Gen(rng, **cfg).build()
    errors = validate(dataset)
    if errors:
        print("VALIDATION FAILED:")
        for e in errors[:20]:
            print("  ", e)
        return 1

    m = dataset["meta"]
    print(f"difficulty={args.difficulty} seed={args.seed}  "
          f"{m['n_sessions']} sessions x {m['turns_per_session']} turns, "
          f"distractor_ratio={m['distractor_ratio']}")
    print(f"{m['n_questions']} questions, ground-truth validated: {m['type_counts']}")

    out = Path(args.out) if args.out else Path(f"results/memeval/{args.difficulty}_s{args.seed}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dataset, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
