"""IPMS — Identity Persistence under Model Substitution.

Benchmark harness that runs a persistent-agent trajectory to a checkpoint,
swaps the backbone LLM while preserving all scaffold state, and measures
whether identity, judgment, and commitments survive. Builds on the
full-fidelity checkpoint primitive in charon.context.context_transfer.
"""
from charon.ipms.harness import (
    Backbone,
    CONDITIONS,
    ConditionResult,
    PairResult,
    Probe,
    TrajectorySpec,
    run_pair,
)

__all__ = [
    'Backbone',
    'CONDITIONS',
    'ConditionResult',
    'PairResult',
    'Probe',
    'TrajectorySpec',
    'run_pair',
]
