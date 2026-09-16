"""Read-only system-map adapter and offline visualization host.

Like Graph Studio, source-checkout assets live under apps/ and the entry point
under scripts/. No Acheron, runtime JavaScript packages, or network are needed.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from charon.workspace.system_map_skill import load_skill_module

ASSETS = Path(__file__).resolve().parents[2] / 'apps' / 'viz'


def project_model(root: Path, map_file: str = 'system-map.json') -> dict:
    """Use exactly the validate/extract/query implementation behind SystemMap.

    Never writes the declaration or inferred descriptions. Observations retain
    separate IDs even when they corroborate a declared dependency.
    """
    root = root.resolve()
    core = load_skill_module('mapcore')
    declared = core.load_map(str(root), map_file)
    validation = core.validate_map(declared, root=str(root))
    if not validation['ok']:
        raise ValueError(core.format_validation(validation))
    derived = core.extract_facts(declared, root=str(root), git=True)
    system = copy.deepcopy(declared['system'])
    system_id = system['id']
    root_id = system_id + '.repo'
    nodes, relationships = [], []

    def reference(locator):
        path, _, symbol = locator.partition('#')
        return {'rootId': root_id, 'path': path, **({'symbol': symbol} if symbol else {})}

    for kind, items in [('subsystem', declared['subsystems']),
                        ('component', declared['components']),
                        ('external', declared.get('externals', []))]:
        for item in items:
            nodes.append({
                'id': item['id'], 'parentId': item.get('subsystem'),
                'kind': item.get('kind', kind), 'name': item['name'],
                'description': item.get('description', ''),
                'code': [reference(p) for p in item.get('code', [])],
                'claim': 'declared', 'freshness': 'unknown',
            })
    for component in declared['components']:
        for target in component.get('depends_on', []):
            relationships.append({
                'id': f'declared:{component["id"]}:{target}',
                'from': {'systemId': system_id, 'nodeId': component['id']},
                'to': {'systemId': system_id, 'nodeId': target},
                'kind': 'dependency', 'label': 'Declared dependency',
                'claim': 'declared', 'freshness': 'unknown', 'evidence': [],
            })
    for relation in derived['relations']:
        relationships.append({
            'id': f'observed:{relation["from"]}:{relation["to"]}:{relation["kind"]}',
            'from': {'systemId': system_id, 'nodeId': relation['from']},
            'to': {'systemId': system_id, 'nodeId': relation['to']},
            'kind': relation['kind'], 'label': 'Observed import',
            'claim': 'observed', 'freshness': 'fresh',
            'evidence': [{'rootId': root_id, 'path': e['file'], 'line': e['line'],
                          'claim': 'observed'} for e in relation['evidence']],
        })
    fingerprint = hashlib.sha256(json.dumps(declared, sort_keys=True).encode()).hexdigest()
    diagnostics = {
        'validation': validation, 'gaps': derived['gaps'], 'conflicts': derived['conflicts'],
        'source_revision': derived['source_revision'], 'extracted_at': derived['extracted_at'],
        'declaration_sha256': fingerprint,
        'summary': core.report_table(declared, derived),
    }
    return {
        'version': 2, 'system': system,
        'revision': fingerprint[:12],
        'roots': [{'id': root_id, 'kind': 'project'}],
        'nodes': nodes, 'relationships': relationships,
        'extensions': {'charon.system-map': {'version': 1, 'fallback': json.dumps(diagnostics, indent=2), 'data': diagnostics}},
    }


def render_html(model: dict, view: dict | None = None) -> str:
    # Escape HTML parser delimiters, including hostile </script> in JSON strings.
    payload = json.dumps({'model': model, 'view': view or {'visibleDepth': 2}}, ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    js = (ASSETS / 'charon-viz.js').read_text(encoding='utf-8')
    css = (ASSETS / 'charon-viz.css').read_text(encoding='utf-8')
    return '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'">
<title>Charon system visualization</title><style>''' + css + '''</style>
<main id="viz"></main><p id="evidence" role="status"></p>
<script type="application/json" id="model">''' + payload + '''</script>
<script type="module">''' + js + '''
const options = JSON.parse(document.getElementById('model').textContent);
options.callbacks = {onOpenEvidence(ref) {
  document.getElementById('evidence').textContent = 'Evidence reference: ' +
    (ref.path || ref.file || ref.url || '') + (ref.line ? ':' + ref.line : '') +
    (ref.symbol ? '#' + ref.symbol : '') + ' (root ' + (ref.rootId || 'unknown') +
    '). Open this reference in your project editor.';
}};
window.charonViz = mount(document.getElementById('viz'), options);
</script></html>'''


def create_server(html: str, port: int = 4318) -> ThreadingHTTPServer:
    """Serve only the generated snapshot, never the project filesystem."""
    body = html.encode('utf-8')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ('/', '/index.html'):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Render a project system map offline')
    parser.add_argument('command', choices=['render', 'serve'])
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--map', default='system-map.json')
    parser.add_argument('--out', type=Path, default=Path('charon-viz.html'))
    parser.add_argument('--port', type=int, default=4318)
    args = parser.parse_args(argv)
    try:
        html = render_html(project_model(args.root, args.map))
        if args.command == 'render':
            args.out.write_text(html, encoding='utf-8')
            print(args.out.resolve())
        else:
            server = create_server(html, args.port)
            print(f'Charon viz: http://127.0.0.1:{server.server_port}', flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        return 0
    except (OSError, ValueError) as exc:
        parser.exit(1, f'Visualization failed: {exc}\n')
