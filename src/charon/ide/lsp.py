"""Small, read-only LSP client. Servers are trusted executables, not sandboxes.

Only disk-backed, project-local UTF-8 documents are supported. Registry sharing
is process-local (including all agent threads); no daemon or dependency install.
Wire/result positions are LSP's zero-based UTF-16; input positions are one-based
Unicode code points. Unversioned push diagnostics never prove freshness.
"""
from __future__ import annotations

import atexit
import json
import math
import os
from pathlib import Path
import queue
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_FRAME = 8 * 1024 * 1024
MAX_DOCUMENT = 2 * 1024 * 1024
MAX_SERVERS = 16
MAX_DOCUMENTS = 64


class LspError(RuntimeError):
    """An unavailable capability, invalid input, or failed server operation."""


def remaining(deadline: float, cancel: threading.Event | None = None) -> float:
    if cancel is not None and cancel.is_set():
        raise LspError('LSP operation cancelled')
    value = deadline - time.monotonic()
    if value <= 0:
        raise LspError('LSP operation timed out')
    return value


def timeout_seconds(value: Any = 10) -> float:
    if isinstance(value, bool):
        raise LspError('timeout must be a number between 0.1 and 30 seconds')
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise LspError('timeout must be a number between 0.1 and 30 seconds') from None
    if not math.isfinite(value) or not 0.1 <= value <= 30:
        raise LspError('timeout must be between 0.1 and 30 seconds')
    return value


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    message: dict[str, Any] | None = None
    error: str | None = None


class JsonRpcClient:
    """Bounded framed stdio transport; no caller blocks on a pipe write.

    A writer queue bounds outstanding writes, the reader bounds headers/payloads,
    and stderr retains only its final 8 KiB. Requests share their caller's absolute
    deadline. A timed-out request sends best-effort $/cancelRequest.
    """

    def __init__(self, command: tuple[str, ...] | list[str], root: Path,
                 on_notification: Callable[[str, Any], None] | None = None):
        self._lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0
        self._failure: str | None = None
        self._outbox: queue.Queue[bytes | None] = queue.Queue(maxsize=32)
        self._notification = on_notification
        self.stderr_tail = bytearray()
        self.process = subprocess.Popen(
            command, cwd=str(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0,
        )
        self._threads = [
            threading.Thread(target=self._reader, name='charon-lsp-reader', daemon=True),
            threading.Thread(target=self._writer, name='charon-lsp-writer', daemon=True),
            threading.Thread(target=self._stderr, name='charon-lsp-stderr', daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    @property
    def alive(self) -> bool:
        return self.process.poll() is None and self._failure is None

    def _fail(self, reason: str) -> None:
        with self._lock:
            self._failure = self._failure or reason
            for pending in self._pending.values():
                pending.error = self._failure
                pending.event.set()

    def send(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=True, separators=(',', ':')).encode('utf-8')
        if len(payload) > MAX_FRAME:
            raise LspError('LSP outbound message exceeds frame limit')
        with self._lock:
            if self._failure:
                raise LspError(self._failure)
        try:
            self._outbox.put_nowait(b'Content-Length: ' + str(len(payload)).encode() + b'\r\n\r\n' + payload)
        except queue.Full:
            raise LspError('LSP write queue is full; server is not consuming input') from None

    def notify(self, method: str, params: Any) -> None:
        self.send({'jsonrpc': '2.0', 'method': method, 'params': params})

    def request(self, method: str, params: Any, deadline: float,
                cancel: threading.Event | None = None) -> Any:
        remaining(deadline, cancel)
        pending = _Pending()
        with self._lock:
            if len(self._pending) >= 64:
                raise LspError('Too many pending LSP requests')
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = pending
        try:
            self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
            while not pending.event.wait(min(0.05, remaining(deadline, cancel))):
                pass
            remaining(deadline, cancel)
            if pending.error:
                raise LspError(pending.error)
            message = pending.message or {}
            if 'error' in message:
                raise LspError(f'LSP {method} failed: {str(message["error"])[:1000]}')
            if 'result' not in message:
                raise LspError(f'LSP {method} returned an invalid response')
            return message['result']
        except LspError:
            try:
                self.notify('$/cancelRequest', {'id': request_id})
            except LspError:
                pass
            raise
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _writer(self) -> None:
        try:
            while True:
                frame = self._outbox.get()
                if frame is None:
                    return
                view = memoryview(frame)
                while view:
                    count = self.process.stdin.write(view)
                    if not count:
                        raise OSError('stdin closed')
                    view = view[count:]
        except (OSError, ValueError) as exc:
            self._fail(f'LSP server write failed: {exc}')

    def _read_exact(self, count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            part = self.process.stdout.read(count - len(data))
            if not part:
                raise EOFError('server closed stdout')
            data.extend(part)
        return bytes(data)

    def _reader(self) -> None:
        try:
            while True:
                header = bytearray()
                while not header.endswith(b'\r\n\r\n'):
                    header.extend(self._read_exact(1))
                    if len(header) > 8192:
                        raise LspError('LSP header exceeds limit')
                lengths = []
                for line in bytes(header).split(b'\r\n'):
                    key, separator, value = line.partition(b':')
                    if separator and key.strip().lower() == b'content-length':
                        lengths.append(int(value.strip()))
                if len(lengths) != 1 or not 0 < lengths[0] <= MAX_FRAME:
                    raise LspError('Invalid or oversized LSP Content-Length')
                message = json.loads(self._read_exact(lengths[0]).decode('utf-8'))
                if not isinstance(message, dict):
                    raise LspError('Invalid LSP message (expected object)')
                if 'method' in message:
                    if 'id' in message:
                        self._server_request(message)
                    elif self._notification:
                        self._notification(message['method'], message.get('params'))
                elif isinstance(message.get('id'), int):
                    with self._lock:
                        pending = self._pending.get(message['id'])
                        if pending:
                            pending.message = message
                            pending.event.set()
        except Exception as exc:
            self._fail(f'LSP server exited or protocol failed: {exc}')

    def _server_request(self, message: dict[str, Any]) -> None:
        method = message.get('method')
        response: dict[str, Any] = {'jsonrpc': '2.0', 'id': message['id']}
        if method == 'workspace/applyEdit':
            response['result'] = {'applied': False, 'failureReason': 'Charon LSP is read-only'}
        elif method == 'workspace/configuration':
            params = message.get('params') or {}
            items = params.get('items', []) if isinstance(params, dict) else []
            response['result'] = [None] * min(len(items), 256)
        elif method == 'workspace/diagnostic/refresh':
            if self._notification:
                self._notification(method, message.get('params'))
            response['result'] = None
        else:
            # Includes workspace/executeCommand, registration, and arbitrary RPCs.
            response['error'] = {'code': -32601, 'message': 'Unsupported by read-only Charon LSP'}
        self.send(response)

    def _stderr(self) -> None:
        try:
            while True:
                data = self.process.stderr.read(4096)
                if not data:
                    return
                with self._lock:
                    self.stderr_tail.extend(data)
                    del self.stderr_tail[:-8192]
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        self._fail('LSP server shut down')
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=0.5)
        try:
            while True:
                self._outbox.get_nowait()
        except queue.Empty:
            pass
        self._outbox.put_nowait(None)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                pipe.close()
            except OSError:
                pass
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=0.1)


@dataclass(frozen=True)
class ServerSpec:
    name: str
    command: tuple[str, ...]
    extensions: tuple[str, ...]


def server_specs(metadata: dict[str, Any] | None) -> tuple[ServerSpec, ...]:
    metadata = metadata or {}
    if 'ide_lsp_servers' in metadata:
        configured = metadata['ide_lsp_servers']
        if not isinstance(configured, list) or len(configured) > MAX_SERVERS:
            raise LspError('ide_lsp_servers must be a list of at most 16 server configurations')
        specs = []
        for item in configured:
            if not isinstance(item, dict):
                raise LspError('Invalid trusted LSP server configuration')
            name, command, extensions = item.get('name'), item.get('command'), item.get('extensions')
            if (not isinstance(name, str) or not name or not isinstance(command, list)
                    or not command or not all(isinstance(x, str) and x and '\0' not in x for x in command)
                    or not isinstance(extensions, list) or not extensions
                    or not all(isinstance(x, str) and x.startswith('.') and '/' not in x and '\\' not in x for x in extensions)):
                raise LspError('Server configuration requires name, command:list[str], extensions:[".ext"]')
            specs.append(ServerSpec(name, tuple(command), tuple(x.lower() for x in extensions)))
        if len({spec.name for spec in specs}) != len(specs):
            raise LspError('LSP server names must be unique')
        return tuple(specs)
    candidates = (
        ('pyright', 'pyright-langserver', ('--stdio',), ('.py', '.pyi')),
        ('rust-analyzer', 'rust-analyzer', (), ('.rs',)),
        ('typescript', 'typescript-language-server', ('--stdio',), ('.ts', '.tsx', '.js', '.jsx', '.mts', '.cts', '.mjs', '.cjs')),
    )
    return tuple(ServerSpec(name, (executable, *args), extensions)
                 for name, binary, args, extensions in candidates
                 if (executable := shutil.which(binary)))


def project_file(root: Path, value: Any) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise LspError('path is required')
    path = Path(value)
    try:
        path = (root / path).resolve(strict=True)
        path.relative_to(root)
        if not path.is_file():
            raise LspError('LSP path must be a regular file')
    except (ValueError, OSError, RuntimeError) as exc:
        raise LspError('LSP path must be an existing regular file inside the project') from exc
    return path


def read_document(root: Path, path: Path) -> str:
    # Recheck containment each time; final-component no-follow avoids opening a
    # newly substituted symlink. Servers themselves remain trusted processes.
    if project_file(root, path) != path:
        raise LspError('LSP file changed identity during access')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise LspError('LSP path must be a regular file')
        if info.st_size > MAX_DOCUMENT:
            raise LspError('LSP document exceeds 2 MiB limit')
        data = stream.read(MAX_DOCUMENT + 1)
        if len(data) > MAX_DOCUMENT:
            raise LspError('LSP document exceeds 2 MiB limit')
    if project_file(root, path) != path:
        raise LspError('LSP file changed identity during access')
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        raise LspError('LSP supports UTF-8 documents only') from None


def position(text: str, line: Any, column: Any) -> dict[str, int]:
    if type(line) is not int or type(column) is not int or line < 1 or column < 1:
        raise LspError('line and column must be positive one-based integers')
    lines = text.split('\n')
    if line > len(lines):
        raise LspError('line is outside the document')
    content = lines[line - 1].removesuffix('\r')
    if column > len(content) + 1:
        raise LspError('column is outside the line (columns count Unicode code points)')
    return {'line': line - 1, 'character': len(content[:column - 1].encode('utf-16-le')) // 2}


def _end_position(text: str) -> dict[str, int]:
    lines = text.split('\n')
    return {'line': len(lines) - 1, 'character': len(lines[-1].encode('utf-16-le')) // 2}


@dataclass
class Document:
    text: str
    version: int = 1
    dirty: bool = False
    diagnostics: list[Any] | None = None


class Manager:
    def __init__(self, root: Path, spec: ServerSpec):
        self.root, self.spec = root, spec
        self.lock = threading.Lock()
        self.changed = threading.Condition()
        self.documents: dict[str, Document] = {}
        self.client: JsonRpcClient | None = None
        self.capabilities: dict[str, Any] = {}
        self.sync_kind = 0
        self.users = 0  # registry-protected leases, including callers waiting for lock

    def _notification(self, method: str, params: Any) -> None:
        with self.changed:
            if method == 'workspace/diagnostic/refresh':
                for doc in self.documents.values():
                    doc.diagnostics = None
            elif method == 'textDocument/publishDiagnostics' and isinstance(params, dict):
                doc = self.documents.get(params.get('uri'))
                if (doc and not doc.dirty and type(params.get('version')) is int
                        and params['version'] == doc.version and isinstance(params.get('diagnostics'), list)):
                    doc.diagnostics = params['diagnostics']
            self.changed.notify_all()

    def start(self, deadline: float, cancel: threading.Event | None) -> None:
        if self.client and self.client.alive:
            return
        if self.client:
            self.client.close()
        self.documents.clear()
        self.client = JsonRpcClient(self.spec.command, self.root, self._notification)
        try:
            response = self.client.request('initialize', {
                'processId': os.getpid(), 'rootUri': self.root.as_uri(),
                'workspaceFolders': [{'uri': self.root.as_uri(), 'name': self.root.name}],
                'capabilities': {
                    'general': {'positionEncodings': ['utf-16']},
                    'workspace': {'applyEdit': False, 'configuration': True},
                    'textDocument': {
                        'publishDiagnostics': {'versionSupport': True},
                        'diagnostic': {'dynamicRegistration': False},
                    },
                },
                'clientInfo': {'name': 'charon-read-only-lsp', 'version': '1'},
            }, deadline, cancel)
            capabilities = response.get('capabilities') if isinstance(response, dict) else None
            if not isinstance(capabilities, dict):
                raise LspError('Invalid LSP initialize response')
            if capabilities.get('positionEncoding', 'utf-16') != 'utf-16':
                raise LspError('Server selected unsupported position encoding (UTF-16 required)')
            self.capabilities = capabilities
            sync = capabilities.get('textDocumentSync', 0)
            self.sync_kind = sync.get('change', 0) if isinstance(sync, dict) else sync
            self.client.notify('initialized', {})
        except Exception:
            self.client.close()
            self.client = None
            raise

    def sync(self, path: Path) -> Document:
        if self.sync_kind not in (1, 2):
            raise LspError('Server does not support disk document synchronization')
        text = read_document(self.root, path)
        uri = path.as_uri()
        with self.changed:
            doc = self.documents.get(uri)
            if doc is None:
                if len(self.documents) >= MAX_DOCUMENTS:
                    raise LspError('LSP open-document limit reached; shutdown to reset')
                doc = Document(text)
                self.documents[uri] = doc
                suffix = path.suffix.lower()
                language = {'.py': 'python', '.pyi': 'python', '.rs': 'rust', '.ts': 'typescript',
                            '.tsx': 'typescriptreact', '.js': 'javascript', '.jsx': 'javascriptreact',
                            '.mts': 'typescript', '.cts': 'typescript', '.mjs': 'javascript', '.cjs': 'javascript'}.get(suffix, suffix.lstrip('.'))
                self.client.notify('textDocument/didOpen', {'textDocument': {
                    'uri': uri, 'languageId': language, 'version': doc.version, 'text': text,
                }})
            elif doc.dirty or doc.text != text:
                change: dict[str, Any] = {'text': text}
                if self.sync_kind == 2:
                    change['range'] = {'start': {'line': 0, 'character': 0}, 'end': _end_position(doc.text)}
                doc.text, doc.version, doc.dirty, doc.diagnostics = text, doc.version + 1, False, None
                self.client.notify('textDocument/didChange', {
                    'textDocument': {'uri': uri, 'version': doc.version}, 'contentChanges': [change],
                })
            return doc

    def diagnostics(self, path: Path, doc: Document, deadline: float,
                    cancel: threading.Event | None) -> dict[str, Any]:
        uri = path.as_uri()
        provider = self.capabilities.get('diagnosticProvider')
        if provider is not None and provider is not False:
            arguments: dict[str, Any] = {'textDocument': {'uri': uri}}
            if isinstance(provider, dict) and isinstance(provider.get('identifier'), str):
                arguments['identifier'] = provider['identifier']
            response = self.client.request('textDocument/diagnostic', arguments, deadline, cancel)
            if not isinstance(response, dict) or response.get('kind') != 'full' or not isinstance(response.get('items'), list):
                raise LspError('Server did not return a full, fresh diagnostic report')
            items, source = response['items'], 'pull'
        else:
            with self.changed:
                while doc.diagnostics is None:
                    if doc.dirty:
                        raise LspError('Diagnostics unavailable: document changed during request')
                    if not self.client.alive:
                        raise LspError('Diagnostics unavailable: LSP server exited')
                    try:
                        delay = min(0.05, remaining(deadline, cancel))
                    except LspError as exc:
                        raise LspError(f'Diagnostics unavailable: no current versioned publication ({exc})') from exc
                    self.changed.wait(delay)
                items, source = doc.diagnostics, 'versioned-push'
        # Disk edits need no hook: compare content again before reporting clean.
        current = read_document(self.root, path)
        with self.changed:
            if doc.dirty or current != doc.text:
                doc.dirty, doc.diagnostics = True, None
                raise LspError('Diagnostics unavailable: document changed during request; retry')
        return {'diagnostics': items, 'source': source, 'version': doc.version, 'fresh': True}

    def run(self, action: str, path: Path, params: dict[str, Any], deadline: float,
            cancel: threading.Event | None) -> dict[str, Any]:
        self.start(deadline, cancel)
        try:
            doc = self.sync(path)
        except Exception:
            # A failed queued didOpen/didChange must not leave local versions
            # looking synchronized on the next query.
            self.client.close()
            self.client = None
            with self.changed:
                self.documents.clear()
            raise
        uri = path.as_uri()
        if action == 'diagnostics':
            result = self.diagnostics(path, doc, deadline, cancel)
        else:
            methods = {
                'definition': ('textDocument/definition', 'definitionProvider'),
                'references': ('textDocument/references', 'referencesProvider'),
                'hover': ('textDocument/hover', 'hoverProvider'),
                'symbols': ('textDocument/documentSymbol', 'documentSymbolProvider'),
            }
            method, capability = methods[action]
            if self.capabilities.get(capability) is None or self.capabilities.get(capability) is False:
                raise LspError(f'LSP server does not support {action}')
            arguments: dict[str, Any] = {'textDocument': {'uri': uri}}
            if action != 'symbols':
                arguments['position'] = position(doc.text, params.get('line'), params.get('column'))
            if action == 'references':
                include = params.get('include_declaration', True)
                if type(include) is not bool:
                    raise LspError('include_declaration must be a boolean')
                arguments['context'] = {'includeDeclaration': include}
            result = {'result': self.client.request(method, arguments, deadline, cancel)}
        return {'server': self.spec.name, 'path': str(path.relative_to(self.root)),
                'position_encoding': 'utf-16', 'result_positions': 'zero-based', **result}

    def close(self, deadline: float) -> None:
        if self.client:
            try:
                if self.client.alive:
                    self.client.request('shutdown', None, min(deadline, time.monotonic() + 0.3))
                    self.client.notify('exit', None)
            except LspError:
                pass
            finally:
                self.client.close()
                self.client = None
        with self.changed:
            self.documents.clear()
            self.changed.notify_all()


_registry_lock = threading.Lock()
_managers: dict[tuple[Path, ServerSpec], Manager] = {}


def _acquire(lock: threading.Lock, deadline: float, cancel: threading.Event | None) -> None:
    while not lock.acquire(timeout=min(0.05, remaining(deadline, cancel))):
        pass


def operate(params: dict[str, Any], ctx: Any, *, existing_only: bool = False) -> dict[str, Any]:
    action = params.get('action')
    if action not in {'status', 'definition', 'references', 'hover', 'symbols', 'diagnostics', 'shutdown'}:
        raise LspError('Unsupported LSP action; only read-only queries, status, and shutdown are allowed')
    allowed = {'action', 'path', 'line', 'column', 'server', 'timeout', 'include_declaration'}
    if set(params) - allowed:
        raise LspError('Unknown LSP parameters; server commands must come from trusted configuration')
    root = Path(ctx.project_root).resolve(strict=True)
    if not root.is_dir():
        raise LspError('LSP project root must be a directory')
    metadata = ctx.metadata or {}
    deadline = time.monotonic() + timeout_seconds(params.get('timeout', metadata.get('ide_lsp_timeout', 10)))
    cancel = getattr(ctx, 'cancel_event', None)
    selected = params.get('server')
    if selected is not None and (not isinstance(selected, str) or not selected):
        raise LspError('server must be a configured server name')
    if action == 'shutdown':
        with _registry_lock:
            managers = [(key, value) for key, value in _managers.items()
                        if key[0] == root and (selected is None or key[1].name == selected)]
        count = 0
        for _key, manager in managers:
            _acquire(manager.lock, deadline, cancel)
            try:
                manager.close(deadline)
                # Keep objects while callers may hold references; idle entries
                # with no query leases can be reclaimed when capacity is needed.
                count += 1
            finally:
                manager.lock.release()
        return {'shutdown': count, 'project': str(root)}
    specs = server_specs(metadata)
    if selected is not None:
        specs = tuple(spec for spec in specs if spec.name == selected)
        if not specs:
            raise LspError('Requested LSP server is not configured or installed')
    if action == 'status':
        with _registry_lock:
            servers = []
            for spec in specs:
                manager = _managers.get((root, spec))
                client = manager.client if manager else None
                servers.append({'name': spec.name, 'extensions': list(spec.extensions),
                                'running': bool(client and client.alive)})
        return {'project': str(root), 'servers': servers, 'available': bool(servers),
                'note': 'Status does not start servers. Installed/trusted servers are not sandboxed.'}
    path = project_file(root, params.get('path'))
    specs = tuple(spec for spec in specs if path.suffix.lower() in spec.extensions)
    if not specs:
        raise LspError(f'No configured or installed language server for {path.suffix or "this file"}; no server was installed')
    key = (root, specs[0])
    with _registry_lock:
        manager = _managers.get(key)
        if manager is None:
            if existing_only:
                raise LspError('No active LSP session; post-edit checks do not start servers')
            if len(_managers) >= MAX_SERVERS:
                for old_key, old in list(_managers.items()):
                    if old.users == 0 and old.client is None and old.lock.acquire(blocking=False):
                        try:
                            del _managers[old_key]
                        finally:
                            old.lock.release()
                        break
            if len(_managers) >= MAX_SERVERS:
                raise LspError('LSP session limit reached; shutdown idle project sessions')
            manager = _managers[key] = Manager(root, specs[0])
        manager.users += 1
    acquired = False
    try:
        _acquire(manager.lock, deadline, cancel)
        acquired = True
        if existing_only and (manager.client is None or not manager.client.alive):
            raise LspError('No active LSP session; post-edit checks do not start servers')
        return manager.run(action, path, params, deadline, cancel)
    finally:
        if acquired:
            manager.lock.release()
        with _registry_lock:
            manager.users -= 1


def notify_file_changed(path: str | Path, ctx: Any) -> int:
    """Mark existing project documents dirty without starting/configuring servers.

    Deletion notifications are allowed; confinement is still enforced. Returns
    the number of open documents invalidated. Does not perform diagnostics.
    """
    root = Path(ctx.project_root).resolve()
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise LspError('Changed LSP path is outside the project') from None
    with _registry_lock:
        managers = [m for (project, _), m in _managers.items() if project == root]
    count = 0
    for manager in managers:
        with manager.changed:
            doc = manager.documents.get(target.as_uri())
            if doc:
                doc.dirty, doc.diagnostics = True, None
                manager.changed.notify_all()
                count += 1
    return count


def _cleanup() -> None:
    with _registry_lock:
        managers = list(_managers.values())
    for manager in managers:
        if manager.client:
            try:
                manager.client.close()
            except Exception:
                pass


atexit.register(_cleanup)
