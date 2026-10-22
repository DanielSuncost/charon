"""Real stdio fixture; intentionally no dependency on implementation framing."""
import json
import subprocess
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else 'normal'
seq = 0
launch_request = None


def send(message):
    global seq
    seq += 1
    message['seq'] = seq
    payload = json.dumps(message).encode()
    frame = ('Content-Length: %d\r\n\r\n' % len(payload)).encode() + payload
    # Fragment across both header and body boundaries.
    for part in (frame[:9], frame[9:24], frame[24:]):
        sys.stdout.buffer.write(part)
        sys.stdout.buffer.flush()


def respond(request, body=None):
    send({'type': 'response', 'request_seq': request['seq'],
          'command': request['command'], 'success': True, 'body': body or {}})


while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if line == b'\r\n':
            break
        key, value = line.decode().split(':', 1)
        headers[key.lower()] = value.strip()
    request = json.loads(sys.stdin.buffer.read(int(headers['content-length'])))
    command = request['command']
    if command == 'initialize':
        if mode == 'launch-timeout':
            time.sleep(60)
        if mode == 'duplicate-length':
            sys.stdout.buffer.write(b'Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}')
            sys.stdout.buffer.flush()
            continue
        respond(request, {'supportsConfigurationDoneRequest': True})
    elif command == 'launch':
        launch_request = request
        send({'type': 'event', 'event': 'initialized'})
    elif command == 'configurationDone':
        respond(request)
        respond(launch_request, {'trusted': launch_request['arguments']})
        send({'type': 'event', 'event': 'stopped', 'body': {'threadId': 1}})
    elif command == 'threads':
        if mode == 'timeout':
            time.sleep(60)
        elif mode == 'exit':
            sys.exit(7)
        elif mode == 'oversize':
            sys.stdout.buffer.write(b'Content-Length: 999999999\r\n\r\n')
            sys.stdout.buffer.flush()
        elif mode == 'header':
            sys.stdout.buffer.write(b'x' * 9000)
            sys.stdout.buffer.flush()
        elif mode == 'malformed':
            sys.stdout.buffer.write(b'Content-Length: 2\r\n\r\n{x')
            sys.stdout.buffer.flush()
        elif mode == 'reverse':
            send({'type': 'request', 'command': 'runInTerminal', 'arguments': {}})
        elif mode == 'flood':
            for _ in range(120):
                send({'type': 'event', 'event': 'output', 'body': {'output': 'x' * 3000}})
            sys.stderr.write('e' * 20000)
            sys.stderr.flush()
            respond(request, {'threads': [{'id': 1, 'name': 'main'}]})
        elif mode == 'child':
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            respond(request, {'child_pid': child.pid})
        else:
            respond(request, {'threads': [{'id': 1, 'name': 'main'}]})
    elif command == 'stackTrace':
        respond(request, {'stackFrames': [{'id': 0, 'name': 'main', 'line': 1, 'column': 1}]})
    elif command == 'scopes':
        respond(request, {'scopes': [{'name': 'locals', 'variablesReference': 1}]})
    elif command == 'variables':
        respond(request, {'variables': [{'name': 'answer', 'value': '42', 'variablesReference': 0}]})
    elif command == 'setBreakpoints':
        respond(request, {'breakpoints': [{'verified': True, 'line': b['line']}
                                        for b in request['arguments']['breakpoints']]})
    else:
        respond(request, {'received': request['arguments']})
