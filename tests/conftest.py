"""Shared pytest configuration.

Two things happen here, both aimed at making the memory/embedding tests
deterministic and CI-friendly:

1. Force the in-process ("local") embedding backend. The default "worker"
   backend spawns a per-state_dir subprocess that loads the model and serves
   embeddings over HTTP. Because each test uses a fresh tmp_path state dir,
   the worker backend spawns a new subprocess per test and is prone to
   RemoteDisconnected flakiness over a long suite. The local backend (now
   process-cached) exercises the same memory logic without that subprocess.

2. Skip the embedding-dependent tests cleanly when sentence-transformers is
   not installed, so the suite stays green in minimal environments.
"""
import os

# Must be set before any test module imports embedding_client / memory_engine.
os.environ.setdefault('CHARON_EMBED_BACKEND', 'local')

import pytest


def _embeddings_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
        return True
    except Exception:
        return False


# Test modules that need real embeddings to run.
_EMBEDDING_MODULES = {
    'test_recall_tool',
    'test_memory_engine',
    'test_context_transfer',
    'test_memory_bridge',
    'test_context_assembler',
}


def pytest_collection_modifyitems(config, items):
    if _embeddings_available():
        return
    skip = pytest.mark.skip(reason='sentence-transformers not installed; embedding tests skipped')
    for item in items:
        if item.module.__name__.split('.')[-1] in _EMBEDDING_MODULES:
            item.add_marker(skip)
