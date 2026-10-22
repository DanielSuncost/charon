"""Cheap post-write invalidation; never start a server or undo a successful edit."""
from __future__ import annotations


def invalidate_after_write(path, ctx) -> None:
    try:
        from charon.ide.lsp import notify_file_changed
        notify_file_changed(path, ctx)
    except Exception as exc:
        # Disk write succeeded. Report the integration fault diagnostically,
        # never turn a successful filesystem mutation into a failed edit result.
        from charon.infra.diagnostics import record
        record('ide', 'could not invalidate language-server document after write', error=exc)
