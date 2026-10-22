"""Deterministic stdlib-only fake LSP subprocess, not imported by production."""
import json
import os
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else 'push'
DOCUMENTS = {}
EDITS = {}
if MODE == 'never-read':
    time.sleep(60)
    sys.exit(0)


def send(message):
    body = json.dumps(message).encode()
    # Fragmentation exercises exact reads rather than assuming pipe read sizes.
    frame = b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body
    sys.stdout.buffer.write(frame[:7])
    sys.stdout.buffer.flush()
    sys.stdout.buffer.write(frame[7:])
    sys.stdout.buffer.flush()


def receive():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b'\r\n':
            break
        if line.lower().startswith(b'content-length:'):
            length = int(line.split(b':')[1])
    return json.loads(sys.stdin.buffer.read(length))


def publication(uri, doc):
    if MODE in ('missing', 'pull', 'slow-pull', 'unchanged'):
        return
    params = {'uri': uri, 'diagnostics': []}
    if 'ERROR' in doc['text']:
        params['diagnostics'] = [{'message': 'fake error', 'severity': 1,
                                 'range': {'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 1}}}]
    if MODE != 'unversioned':
        params['version'] = doc['version'] - (1 if MODE == 'stale' else 0)
    if MODE == 'old-clean' and doc['version'] > 1:
        params['version'] -= 1
        params['diagnostics'] = []
    send({'jsonrpc': '2.0', 'method': 'textDocument/publishDiagnostics', 'params': params})


while True:
    message = receive()
    if message is None:
        break
    method = message.get('method')
    params = message.get('params') or {}
    if method is None:
        EDITS[str(message['id'])] = message.get('result', message.get('error'))
        continue
    if method == 'exit':
        break
    if method == 'initialize':
        if MODE == 'exit':
            os._exit(7)
        if MODE == 'bad-frame':
            sys.stdout.buffer.write(b'Content-Length: 999999999\r\n\r\n')
            sys.stdout.buffer.flush()
            continue
        if MODE == 'stderr':
            sys.stderr.buffer.write(b'x' * (1024 * 1024))
            sys.stderr.buffer.flush()
        capabilities = {'textDocumentSync': {'openClose': True, 'change': 2},
                        'hoverProvider': True, 'definitionProvider': True,
                        'referencesProvider': True, 'documentSymbolProvider': True}
        if MODE in ('pull', 'slow-pull', 'unchanged'):
            capabilities['diagnosticProvider'] = {}
        if MODE == 'unsupported':
            capabilities['hoverProvider'] = False
        if MODE == 'utf8':
            capabilities['positionEncoding'] = 'utf-8'
        result = {'capabilities': capabilities}
    elif method == 'initialized':
        if MODE == 'edits':
            send({'jsonrpc': '2.0', 'id': 'apply', 'method': 'workspace/applyEdit',
                  'params': {'edit': {'changes': {}}}})
            send({'jsonrpc': '2.0', 'id': 'command', 'method': 'workspace/executeCommand',
                  'params': {'command': 'evil'}})
        continue
    elif method == 'textDocument/didOpen':
        doc = params['textDocument']
        DOCUMENTS[doc['uri']] = doc
        publication(doc['uri'], doc)
        continue
    elif method == 'textDocument/didChange':
        uri = params['textDocument']['uri']
        old = DOCUMENTS[uri]
        change = params['contentChanges'][0]
        # Assert full replacement is encoded as a valid incremental range.
        assert change['range']['start'] == {'line': 0, 'character': 0}
        lines = old['text'].split('\n')
        assert change['range']['end'] == {'line': len(lines) - 1,
                                         'character': len(lines[-1].encode('utf-16-le')) // 2}
        old.update(version=params['textDocument']['version'], text=change['text'])
        publication(uri, old)
        continue
    elif method == '$/cancelRequest':
        send({'jsonrpc': '2.0', 'method': 'test/cancelled', 'params': params})
        continue
    elif method == 'textDocument/diagnostic':
        if MODE == 'slow-pull':
            time.sleep(0.25)
        result = {'kind': 'unchanged', 'resultId': 'old'} if MODE == 'unchanged' else {'kind': 'full', 'items': []}
    elif method == 'textDocument/hover':
        if MODE == 'hang':
            continue
        if MODE == 'crash-query':
            os._exit(9)
        doc = DOCUMENTS[params['textDocument']['uri']]
        result = {'contents': {'kind': 'plaintext', 'value': json.dumps({
            'position': params['position'], 'version': doc['version'],
            'text': doc['text'], 'server_responses': EDITS,
        })}}
    elif method in ('textDocument/definition', 'textDocument/references'):
        point = params['position']
        result = [{'uri': params['textDocument']['uri'], 'range': {'start': point, 'end': point}}]
    elif method == 'textDocument/documentSymbol':
        result = [{'name': 'fake_symbol', 'kind': 12, 'range': {'start': {'line': 0, 'character': 0},
                   'end': {'line': 0, 'character': 1}}, 'selectionRange': {'start': {'line': 0, 'character': 0},
                   'end': {'line': 0, 'character': 1}}}]
    elif method == 'shutdown':
        result = None
    else:
        if 'id' in message:
            send({'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32601, 'message': 'unknown'}})
        continue
    if 'id' in message:
        send({'jsonrpc': '2.0', 'id': message['id'], 'result': result})
