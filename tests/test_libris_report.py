"""Libris HTML report renderer: markdown conversion, epistemic model, and a
full render over a synthetic operation directory."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))

import libris_report as lr


def test_markdown_basics():
    h = lr.markdown_to_html(
        '# Title\n\nA **bold** and `code` and [x](https://e.com).\n\n'
        '- one\n- two\n\n1. first\n2. second\n\n> a quote'
    )
    assert '<h1>Title</h1>' in h
    assert '<strong>bold</strong>' in h and '<code>code</code>' in h
    assert '<a href="https://e.com"' in h
    assert '<ul>' in h and '<ol>' in h and h.count('<li>') == 4
    assert '<blockquote>' in h


def test_markdown_escapes_html():
    h = lr.markdown_to_html('a <script>alert(1)</script> b')
    assert '<script>' not in h
    assert '&lt;script&gt;' in h


def test_epistemic_summary_and_contested():
    claims = [
        {'confidence': 'high', 'stance': 'supports', 'entity_refs': ['dopamine']},
        {'confidence': 'low', 'stance': 'contradicts', 'entity_refs': ['dopamine']},
        {'confidence': 'medium', 'stance': 'supports', 'entity_refs': ['cortex']},
    ]
    epi = lr.epistemic_summary(claims)
    assert epi['total'] == 3
    assert epi['confidence'] == {'high': 1, 'medium': 1, 'low': 1}
    assert epi['stance']['contradicts'] == 1
    assert epi['contested'] == ['dopamine']  # supports+contradicts on same entity


def _build_operation(tmp_path):
    op_id = 'rop_test'
    rroot = tmp_path / 'research'
    op_dir = rroot / 'operations' / op_id
    (op_dir / 'topics' / 'skill-vs-language').mkdir(parents=True)
    (rroot / 'sources').mkdir(parents=True, exist_ok=True)

    (op_dir / 'operation.json').write_text(json.dumps({
        'operation_id': op_id, 'prompt': 'RL in the brain: skill vs language',
        'status': 'reports_ready',
        'usage': {'total_tokens': 120000, 'estimated_cost_usd': 0.42},
    }))
    (op_dir / 'topics' / 'skill-vs-language' / 'topic.json').write_text(json.dumps({
        'slug': 'skill-vs-language', 'title': 'RL in skill vs language learning',
        'why_interesting': 'Contested whether prediction-error RL underlies both.',
        'status': 'checkpointed', 'focus_questions': ['Does dopamine encode RPE in both?'],
    }))
    (op_dir / 'topics' / 'skill-vs-language' / 'draft-report.md').write_text(
        '# Report\n\n## Summary\nDopaminergic **RPE** signals are well established in '
        'motor skill learning. Their role in language is [debated](https://arxiv.org/abs/1234.5678).\n'
    )
    src = {'source_id': 'src_a', 'operation_id': op_id, 'topic_slug': 'skill-vs-language',
           'url': 'https://arxiv.org/abs/1234.5678', 'title': 'Dopamine and RPE',
           'source_type': 'paper', 'authors': ['A. Author', 'B. Author', 'C. Author', 'D. Author'],
           'published_at': '2024-01-01', 'credibility': 'high'}
    (rroot / 'sources' / 'sources.jsonl').write_text(json.dumps(src) + '\n')
    claims = [
        {'operation_id': op_id, 'topic_slug': 'skill-vs-language', 'source_id': 'src_a',
         'text': 'Dopamine encodes reward-prediction error during motor skill learning.',
         'confidence': 'high', 'stance': 'supports', 'evidence_grade': 'strong',
         'entity_refs': ['dopamine', 'skill learning']},
        {'operation_id': op_id, 'topic_slug': 'skill-vs-language', 'source_id': 'src_a',
         'text': 'RPE drives language acquisition the same way it drives skills.',
         'confidence': 'low', 'stance': 'contradicts', 'evidence_grade': 'contested',
         'entity_refs': ['dopamine', 'language learning']},
    ]
    (rroot / 'claims.jsonl').write_text('\n'.join(json.dumps(c) for c in claims) + '\n')
    return op_dir


def test_render_operation_full(tmp_path):
    op_dir = _build_operation(tmp_path)
    html = lr.render_operation(op_dir, title='RL in the brain',
                               verified={'src_a': True})

    assert html.startswith('<!doctype html>') and html.rstrip().endswith('</html>')
    # header + question
    assert 'RL in the brain' in html
    # both claims rendered as cards with confidence + stance badges
    assert html.count('class="claim claim-') == 2
    assert 'confidence: high' in html and 'confidence: low' in html
    assert 'badge-stance-contradicts' in html
    # evidence grade + contested surfaced
    assert 'Strong evidence' in html and 'Contested' in html
    # citation with author truncation + verified badge
    assert 'et al.' in html
    assert 'badge-verified' in html
    # claim links to its citation
    assert 'href="#src-1"' in html and 'id="src-1"' in html
    # report body markdown converted
    assert '<strong>RPE</strong>' in html
    # epistemic tiles show the 1 contradicting claim and 1 contested entity
    assert 'contradicting claims' in html and 'contested entities' in html
    # no raw template leakage
    assert '>False<' not in html and '{tiles' not in html


def test_render_handles_empty_operation(tmp_path):
    op_dir = tmp_path / 'research' / 'operations' / 'rop_empty'
    op_dir.mkdir(parents=True)
    (op_dir / 'operation.json').write_text(json.dumps({'operation_id': 'rop_empty', 'prompt': 'x'}))
    html = lr.render_operation(op_dir)
    assert html.startswith('<!doctype html>')  # degrades gracefully, no crash
