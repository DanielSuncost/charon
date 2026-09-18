"""Shared fixture suite runs in real offline headless Chromium via Playwright."""
import copy
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from charon.viz import ASSETS, create_server, project_model, render_html


@pytest.fixture(scope='module')
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(browser, tmp_path):
    context = browser.new_context(offline=True)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    fixture = json.loads((ASSETS / 'fixtures/small.json').read_text())
    html = render_html(fixture['model'], fixture['view']).replace('const options = JSON.parse', 'window.mount = mount; window.layoutSystem = layoutSystem; const options = JSON.parse')
    path = tmp_path / 'viz.html'
    path.write_text(html)
    page.goto(path.as_uri())
    page.wait_for_function('window.charonViz !== undefined')
    yield page
    context.close()
    assert errors == []


@pytest.mark.parametrize('path', sorted((ASSETS / 'fixtures').glob('*.json')), ids=lambda p: p.stem)
def test_viz_shared_fixtures(page, path):
    fixture = json.loads(path.read_text())
    assert page.evaluate('(v) => charonViz.update(v)', fixture)
    assert page.locator('.viz-error').inner_text() == ''
    assert 0 < page.locator('.viz-node').count() <= 200
    assert 'revision' in page.evaluate('charonViz.exportText()')
    assert 'Claims:' in page.evaluate('charonViz.exportSVG()')
    if path.stem in ('unresolved', 'several-systems'):
        assert 'Unresolved reference' in page.locator('main').inner_text()
    if path.stem == 'unknown-extension':
        page.get_by_text('Extension example.future v99 — fallback', exact=True).click()
        assert 'Unsupported temperature' in page.locator('pre').inner_text()
    if path.stem == 'several-systems':
        assert page.evaluate('(model) => charonViz.update({model})', fixture['systems'][1])
        assert page.locator('.viz-node').count() == 1
    if path.stem == 'mixed-status':
        for status in ('declared · fresh', 'observed · stale', 'inferred · unknown'):
            assert status in page.locator('svg').text_content()
    if path.stem == 'deep-100':
        assert page.locator('.viz-node').count() == 100
        page.evaluate("charonViz.focus('n99')")
        assert page.locator('nav button').count() == 101
    if path.stem == 'large-10000':
        assert page.locator('.viz-node').count() == 200
        assert '9800 nodes not drawn' in page.locator('.viz-limit').inner_text()
        timing = page.evaluate("() => { const t = performance.now(); charonViz.select('n1'); return performance.now() - t; }")
        print(f'10,000-node select: {timing:.2f} ms')


def test_viz_failed_update_recovery_positions_state_and_theme(page):
    fixture = json.loads((ASSETS / 'fixtures/small.json').read_text())
    page.evaluate("charonViz.select('b')")
    old = page.locator('[data-id="b"]').get_attribute('transform')
    before = page.evaluate('charonViz.getState()')
    for damage in ('cycle', 'missing', 'self', 'schema', 'layout'):
        bad = copy.deepcopy(fixture)
        if damage == 'cycle':
            bad['model']['nodes'][0]['parentId'] = 'b'
        elif damage == 'missing':
            bad['model']['nodes'][0]['parentId'] = 'absent'
        elif damage == 'self':
            bad['model']['nodes'][0]['parentId'] = 'a'
        elif damage == 'schema':
            bad['model']['version'] = 99
        else:
            bad['view']['pinnedPositions'] = {'a': {'x': 'no', 'y': 1}}
        assert not page.evaluate('(v) => charonViz.update(v)', bad)
        assert page.locator('.viz-node').count() == 3
        assert page.evaluate('charonViz.getState()') == before
        assert 'error' in page.locator('.viz-error').inner_text()
    fixture['model']['nodes'].append(dict(id='new', parentId='a', name='New', kind='component'))
    assert page.evaluate('(v) => charonViz.update(v)', fixture)
    assert page.locator('[data-id="b"]').get_attribute('transform') == old
    assert page.evaluate('charonViz.getState()') == before
    fixture['view']['pinnedPositions'] = {'b': {'x': 730, 'y': 450}}
    page.evaluate('(v) => charonViz.update(v)', fixture)
    assert page.locator('[data-id="b"]').get_attribute('transform') == 'translate(730,450)'
    page.evaluate('charonViz.update({})')
    assert page.locator('[data-id="b"]').get_attribute('transform') == 'translate(730,450)'
    page.evaluate('charonViz.autoLayout()')
    assert page.locator('[data-id="b"]').get_attribute('transform') != 'translate(730,450)'
    page.evaluate("document.querySelector('main').style.setProperty('--viz-text', 'rgb(1, 2, 3)')")
    assert page.locator('.charon-viz').evaluate('(e) => getComputedStyle(e).color') == 'rgb(1, 2, 3)'


def test_viz_untrusted_text_evidence_and_exports(page):
    fixture = json.loads((ASSETS / 'fixtures/small.json').read_text())
    fixture['model']['nodes'][0]['name'] = '</script><img src=x onerror="window.pwned=1">'
    assert page.evaluate('(v) => charonViz.update(v)', fixture)
    assert page.evaluate('window.pwned') is None
    assert page.locator('img').count() == 0
    page.evaluate("charonViz.select('bc')")
    page.get_by_role('button', name='src/main.py:12', exact=True).click()
    assert 'src/main.py:12' in page.locator('#evidence').inner_text()
    svg = page.evaluate('charonViz.exportSVG()')
    assert '&lt;/script&gt;' in svg and 'stroke:' in svg
    assert 'src/main.py:12' in page.evaluate('charonViz.exportText()')


def test_viz_lazy_callbacks_and_destroy(page):
    page.evaluate('''() => {
      const model = JSON.parse(document.getElementById('model').textContent).model;
      charonViz.destroy(); window.intents = [];
      window.charonViz = mount(document.getElementById('viz'), {model, view: {visibleDepth: 2}, callbacks: {
        onRequestChildren: () => new Promise((resolve, reject) => { window.resolveChildren = resolve; window.rejectChildren = reject; }),
        onEditIntent: op => intents.push(op)
      }});
      charonViz.select('a');
    }''')
    page.get_by_role('button', name='Expand children').click()
    assert 'Loading children' in page.locator('.viz-error').inner_text()
    page.evaluate("rejectChildren(Error('children unavailable'))")
    assert 'children unavailable' in page.locator('.viz-error').inner_text()
    assert page.locator('.viz-node').count() == 3
    page.get_by_role('button', name='Expand children').click()
    page.evaluate("resolveChildren({nodes:[{id:'lazy',parentId:'a',name:'Lazy'}],relationships:[]})")
    assert page.locator('.viz-node').count() == 4
    page.get_by_role('textbox', name='New description').fill('Proposal only')
    page.get_by_role('button', name='Propose description').click()
    assert page.evaluate('intents[0].value') == 'Proposal only'
    assert 'Proposal only' not in page.evaluate('charonViz.exportText()')
    page.get_by_role('button', name='Expand children').click()
    page.evaluate('charonViz.destroy(); resolveChildren({nodes:[]})')
    assert page.locator('.charon-viz').count() == 0


def test_viz_adapter_preserves_declaration_and_observations(tmp_path):
    root = Path(__file__).resolve().parents[1]
    path = ASSETS / 'charon-example-map.json'
    before = path.read_bytes()
    model = project_model(root, str(path))
    assert path.read_bytes() == before
    assert model['relationships']
    assert all(r['claim'] == 'observed' and r['evidence'][0]['line'] > 0 for r in model['relationships'])
    assert all(n['claim'] == 'declared' for n in model['nodes'])
    assert model['extensions']['charon.system-map']['data']['gaps']
    assert model['extensions']['charon.system-map']['data']['conflicts']
    assert next(n for n in model['nodes'] if n['id'] == 'tools')['description'] == 'Tools implementation'
    declared = json.loads(before)
    observed = model['relationships'][0]
    component = next(c for c in declared['components'] if c['id'] == observed['from']['nodeId'])
    component['depends_on'] = [observed['to']['nodeId']]
    custom = tmp_path / 'map.json'
    custom.write_text(json.dumps(declared))
    refreshed = project_model(root, str(custom))
    same_pair = [r for r in refreshed['relationships'] if r['from'] == observed['from'] and r['to'] == observed['to']]
    assert {r['claim'] for r in same_pair} == {'declared', 'observed'}


def test_viz_html_script_escape():
    html = render_html({'system': {'name': '</script><script>alert(1)</script>'}})
    assert '</script><script>alert' not in html
    assert '\\u003c/script\\u003e' in html


def test_viz_loopback_server():
    server = create_server('<h1>Snapshot</h1>', 0)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}'
        with urllib.request.urlopen(url) as response:
            assert response.read() == b'<h1>Snapshot</h1>'
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(url + '/system-map.json')
        assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_viz_semantic_columns_crossings_and_determinism(page):
    fixture = json.loads((ASSETS / 'fixtures/crossing-reduction.json').read_text())
    metric = page.evaluate('''({model}) => {
      const idx = {nodes: new Map(model.nodes.map(n => [n.id,n]))};
      const a = layoutSystem(model.nodes, model.relationships, idx);
      const b = layoutSystem(model.nodes, model.relationships, idx);
      return {baseline: a.baselineCrossings, reduced: a.actualCrossings,
        first: [...a.pos], second: [...b.pos]};
    }''', fixture)
    assert metric['first'] == metric['second']
    assert metric['baseline'] == 6
    assert metric['reduced'] == 0
    print(f"Crossing fixture: {metric['baseline']} → {metric['reduced']} inversions")
    page.evaluate('(f) => charonViz.update(f)', fixture)
    positions = page.evaluate('''() => Object.fromEntries([...document.querySelectorAll('.viz-node')].map(n => [n.dataset.id, n.getAttribute('transform')]))''')
    # Existing fixture positions survive updates. Start a fresh layout to check columns.
    page.evaluate('charonViz.autoLayout()')
    assert 'left → right: depends on' in page.locator('.viz-axis').inner_text()
    geometry = page.evaluate('''() => Object.fromEntries([...document.querySelectorAll('.viz-node')].map(n => [n.dataset.id, n.transform.baseVal.getItem(0).matrix.e]))''')
    for a, b in [('a', 'z'), ('b', 'y'), ('c', 'x'), ('d', 'w')]:
        assert geometry[a] < geometry[b]
    positions = page.evaluate('''() => Object.fromEntries([...document.querySelectorAll('.viz-node')].map(n => [n.dataset.id, n.getAttribute('transform')]))''')
    fixture['model']['nodes'].append(dict(id='unrelated', parentId=None, kind='component', name='Unrelated'))
    page.evaluate('(f) => charonViz.update(f)', fixture)
    after = page.evaluate('''() => Object.fromEntries([...document.querySelectorAll('.viz-node')].map(n => [n.dataset.id, n.getAttribute('transform')]))''')
    assert all(after[id] == p for id, p in positions.items())


def test_viz_flow_pins_feedback_and_regions(page):
    flow = json.loads((ASSETS / 'fixtures/flow.json').read_text())
    page.evaluate('(f) => charonViz.update(f)', flow)
    assert 'Flow view' in page.locator('.viz-axis').inner_text()
    coords = page.evaluate('''() => [...document.querySelectorAll('.viz-node')].map(n => n.transform.baseVal.getItem(0).matrix.e)''')
    assert coords == sorted(coords) and len(set(coords)) == 5
    flow['view']['pinnedPositions'] = {'reader': {'x': 150, 'y': 300}}
    page.evaluate('(f) => charonViz.update(f)', flow)
    assert page.locator('[data-id="reader"]').get_attribute('transform') == 'translate(150,300)'
    page.evaluate("charonViz.update({view:{mode:'overview',pinnedPositions:{reader:{x:150,y:300}}}})")
    assert page.locator('[data-id="reader"]').get_attribute('transform') == 'translate(150,300)'
    page.evaluate('charonViz.autoLayout()')
    assert page.locator('[data-id="reader"]').get_attribute('transform') != 'translate(150,300)'
    cycle = json.loads((ASSETS / 'fixtures/relationship-cycle.json').read_text())
    page.evaluate('(f) => charonViz.update(f)', cycle)
    page.evaluate('charonViz.autoLayout()')
    assert page.locator('.viz-feedback').count() > 0
    assert 'feedback' in page.locator('.viz-feedback-label').first.text_content()
    # Two subsystem lanes, a cross-lane edge via outer boundary rails.
    model = cycle['model']
    model['nodes'][0]['kind'] = 'subsystem'
    model['nodes'].append(dict(id='other',parentId=None,kind='subsystem',name='Other'))
    model['nodes'][2]['parentId'] = 'other'
    page.evaluate('(f) => charonViz.update(f)', cycle)
    page.evaluate('charonViz.autoLayout()')
    assert page.locator('.viz-lane').count() == 2
    path = page.locator('[data-edge-id="bc"]').get_attribute('d')
    assert path.count('H') == 4 and path.count('V') == 3
