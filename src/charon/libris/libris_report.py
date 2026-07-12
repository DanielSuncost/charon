"""Libris report renderer — turn a completed research operation into a single,
self-contained, shareable HTML report.

Reads a Libris operation directory (operation.json, per-topic draft reports,
the project claims.jsonl / sources.jsonl, and judge checkpoints) and emits one
standalone HTML file: no external assets, theme-aware, with per-claim epistemic
grading (confidence + stance + evidence grade), a linked citations panel with
source-type and verification badges, and the judge's scorecard.

The renderer is presentation only — it never invents content. Every claim card
and citation traces to a row the research agents actually saved. Claims whose
source could not be verified (see verify_sources) are flagged, not hidden.

CLI:
  python libris_report.py <operation_dir> [--out report.html] [--title "..."]
"""
from __future__ import annotations

import argparse
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


# ── data loading ──────────────────────────────────────────────────────────

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        _diag('libris_report', 'operation JSON unreadable; rendering with default value', error=e, path=str(path))
        return default


def _iter_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def _research_root(operation_dir: Path) -> Path:
    # operation_dir = <research>/operations/<op-id>; research root is two up.
    return operation_dir.parent.parent


def load_operation(operation_dir: Path) -> dict[str, Any]:
    """Collect everything the renderer needs from a Libris operation directory."""
    operation_dir = Path(operation_dir)
    op = _read_json(operation_dir / 'operation.json', {})
    op_id = op.get('operation_id') or operation_dir.name
    rroot = _research_root(operation_dir)

    all_claims = _iter_jsonl(rroot / 'claims.jsonl')
    all_sources = _iter_jsonl(rroot / 'sources' / 'sources.jsonl')
    claims = [c for c in all_claims if c.get('operation_id') == op_id]
    sources = [s for s in all_sources if s.get('operation_id') == op_id]
    sources_by_id = {s.get('source_id'): s for s in sources}

    topics = []
    troot = operation_dir / 'topics'
    if troot.exists():
        for tdir in sorted(p for p in troot.iterdir() if p.is_dir()):
            tj = _read_json(tdir / 'topic.json', {})
            slug = tj.get('slug') or tdir.name
            report_md = ''
            dr = tdir / 'draft-report.md'
            if dr.exists():
                report_md = dr.read_text(encoding='utf-8')
            # prefer the latest judged checkpoint report if present
            ckpts = _load_checkpoints(tdir)
            if ckpts and ckpts[-1].get('report_md'):
                report_md = ckpts[-1]['report_md']
            topics.append({
                'slug': slug,
                'title': tj.get('title') or slug,
                'why': tj.get('why_interesting') or '',
                'status': tj.get('status') or '',
                'focus_questions': tj.get('focus_questions') or [],
                'report_md': report_md,
                'claims': [c for c in claims if c.get('topic_slug') == slug],
                'sources': [s for s in sources if s.get('topic_slug') == slug],
                'checkpoints': ckpts,
            })

    return {
        'operation_id': op_id,
        'prompt': op.get('prompt') or '',
        'summary': op.get('summary') or '',
        'status': op.get('status') or '',
        'usage': op.get('usage') or {},
        'created_at': op.get('created_at') or '',
        'topics': topics,
        'claims': claims,
        'sources': sources,
        'sources_by_id': sources_by_id,
    }


def _load_checkpoints(tdir: Path) -> list[dict]:
    out = []
    cdir = tdir / 'checkpoints'
    if not cdir.exists():
        return out
    for meta_path in sorted(cdir.glob('*-meta.json')):
        meta = _read_json(meta_path, {})
        for key, fname in (('report_md', 'report_path'),
                           ('critique_md', 'critique_path'),
                           ('summary_md', 'summary_path')):
            p = meta.get(fname)
            if p and Path(p).exists():
                meta[key] = Path(p).read_text(encoding='utf-8')
        out.append(meta)
    # fallback: bare report files without meta
    if not out:
        for rep in sorted(cdir.glob('*-report.md')):
            out.append({'report_md': rep.read_text(encoding='utf-8')})
    return out


# ── minimal, safe markdown → HTML ─────────────────────────────────────────

_INLINE_CODE = re.compile(r'`([^`]+)`')
_BOLD = re.compile(r'\*\*([^*]+)\*\*')
_ITALIC = re.compile(r'(?<![\*\w])\*([^*\n]+)\*(?!\*)')
_LINK = re.compile(r'\[([^\]]+)\]\((https?://[^\s)]+)\)')
_BARE_URL = re.compile(r'(?<!["\'=(>])(https?://[^\s<)]+)')


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = _INLINE_CODE.sub(lambda m: f'<code>{m.group(1)}</code>', text)
    text = _LINK.sub(lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>', text)
    text = _BARE_URL.sub(lambda m: f'<a href="{m.group(1)}" target="_blank" rel="noopener">{m.group(1)}</a>', text)
    text = _BOLD.sub(lambda m: f'<strong>{m.group(1)}</strong>', text)
    text = _ITALIC.sub(lambda m: f'<em>{m.group(1)}</em>', text)
    return text


def markdown_to_html(md: str) -> str:
    """Compact renderer covering the structures Libris reports actually use:
    ATX headings, unordered/ordered lists, blockquotes, hr, paragraphs, and the
    inline set above. Not a general markdown engine — deliberately small."""
    lines = md.replace('\r\n', '\n').split('\n')
    out: list[str] = []
    list_stack: list[str] = []  # 'ul' | 'ol'

    def close_lists():
        while list_stack:
            out.append(f'</{list_stack.pop()}>')

    para: list[str] = []

    def flush_para():
        if para:
            out.append(f'<p>{_inline(" ".join(para).strip())}</p>')
            para.clear()

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_para()
            close_lists()
            continue
        h = re.match(r'^(#{1,6})\s+(.*)$', stripped)
        if h:
            flush_para()
            close_lists()
            level = len(h.group(1))
            out.append(f'<h{level}>{_inline(h.group(2).strip())}</h{level}>')
            continue
        if re.match(r'^(-{3,}|\*{3,}|_{3,})$', stripped):
            flush_para()
            close_lists()
            out.append('<hr>')
            continue
        ol = re.match(r'^\d+[.)]\s+(.*)$', stripped)
        ul = re.match(r'^[-*+]\s+(.*)$', stripped)
        if ol or ul:
            flush_para()
            want = 'ol' if ol else 'ul'
            if not list_stack or list_stack[-1] != want:
                close_lists()
                out.append(f'<{want}>')
                list_stack.append(want)
            out.append(f'<li>{_inline((ol or ul).group(1).strip())}</li>')
            continue
        if stripped.startswith('>'):
            flush_para()
            close_lists()
            out.append(f'<blockquote>{_inline(stripped.lstrip("> ").strip())}</blockquote>')
            continue
        para.append(stripped)

    flush_para()
    close_lists()
    return '\n'.join(out)


# ── epistemic model ───────────────────────────────────────────────────────

_CONF_ORDER = {'high': 3, 'medium': 2, 'moderate': 2, 'low': 1, 'unknown': 0}
_GRADE_LABELS = {
    'strong': 'Strong evidence', 'moderate': 'Moderate evidence',
    'weak': 'Weak evidence', 'anecdotal': 'Anecdotal', 'contested': 'Contested',
    'theoretical': 'Theoretical',
}


def _contested_entities(claims: list[dict]) -> set[str]:
    """Entities that appear in both a supporting and a contradicting claim."""
    supports, contradicts = set(), set()
    for c in claims:
        refs = [str(r).lower() for r in (c.get('entity_refs') or [])]
        stance = str(c.get('stance') or 'supports').lower()
        target = contradicts if stance in ('contradicts', 'contradict') else supports
        target.update(refs)
    return supports & contradicts


def epistemic_summary(claims: list[dict]) -> dict[str, Any]:
    conf = {'high': 0, 'medium': 0, 'low': 0}
    stance = {'supports': 0, 'contradicts': 0, 'unclear': 0}
    for c in claims:
        cv = str(c.get('confidence') or 'unknown').lower()
        cv = 'medium' if cv == 'moderate' else cv
        if cv in conf:
            conf[cv] += 1
        sv = str(c.get('stance') or 'supports').lower()
        if sv.startswith('contradict'):
            stance['contradicts'] += 1
        elif sv.startswith('support'):
            stance['supports'] += 1
        else:
            stance['unclear'] += 1
    return {'total': len(claims), 'confidence': conf, 'stance': stance,
            'contested': sorted(_contested_entities(claims))}


# ── HTML rendering ────────────────────────────────────────────────────────

def _badge(text: str, kind: str) -> str:
    return f'<span class="badge badge-{kind}">{html.escape(text)}</span>'


def _source_citation(src: dict, num: int, verified: dict | None) -> str:
    title = html.escape(src.get('title') or '(untitled source)')
    url = src.get('url') or ''
    stype = (src.get('source_type') or 'web').lower()
    authors = src.get('authors') or []
    author_str = ''
    if authors:
        shown = ', '.join(html.escape(a) for a in authors[:3])
        if len(authors) > 3:
            shown += ' et al.'
        author_str = f'<div class="cite-authors">{shown}</div>'
    pub = html.escape(str(src.get('published_at') or ''))
    cred = str(src.get('credibility') or 'unknown').lower()
    link = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>' if url else title
    vstate = ''
    if verified is not None:
        v = verified.get(src.get('source_id'))
        if v is True:
            vstate = _badge('verified', 'verified')
        elif v is False:
            vstate = _badge('unverified', 'unverified')
    meta = ' · '.join(x for x in [stype, pub] if x)
    return (
        f'<li id="src-{num}" class="cite">'
        f'<span class="cite-num">[{num}]</span>'
        f'<div class="cite-body"><div class="cite-title">{link} {_badge(stype, "type")} '
        f'{_badge("cred: " + cred, "cred-" + cred)} {vstate}</div>'
        f'{author_str}'
        f'<div class="cite-meta">{html.escape(meta)}</div></div></li>'
    )


_CITE_TOKEN = re.compile(r'\[cite:\s*([a-zA-Z0-9_,\s-]+?)\s*\]')
# Fallback sweep for any residual cite-shaped token the resolver didn't handle —
# malformed/placeholder ones an agent may leave in prose, e.g. "[cite:...]" or
# "[cite:<id>]" (its content survives html-escaping so the strict regex misses
# it). These must never render as raw text.
_CITE_RESIDUAL = re.compile(r'\[cite:[^\]\n]{0,120}\]?')


def _apply_cite_tokens(html_str: str, src_num: dict) -> str:
    """Replace inline `[cite:src_id]` / `[cite:src_a,src_b]` tokens (written by the
    research/writer agents, who know source_ids but not render-time numbers) with
    numbered superscript links into the citations panel. Unknown ids are dropped,
    and any residual cite-shaped token (malformed/placeholder) is stripped, so a
    token never renders as raw text."""
    def repl(m):
        links = []
        for sid in (s.strip() for s in m.group(1).split(',')):
            n = src_num.get(sid)
            if n:
                links.append(f'<a href="#src-{n}">{n}</a>')
        if not links:
            return ''
        return '<sup class="cite-ref">[' + ', '.join(links) + ']</sup>'
    html_str = _CITE_TOKEN.sub(repl, html_str)
    return _CITE_RESIDUAL.sub('', html_str)  # drop anything cite-shaped left over


def _claim_card(claim: dict, src_num: dict, contested: set[str]) -> str:
    conf = str(claim.get('confidence') or 'unknown').lower()
    conf = 'medium' if conf == 'moderate' else conf
    stance = str(claim.get('stance') or 'supports').lower()
    stance_key = ('contradicts' if stance.startswith('contradict')
                  else 'supports' if stance.startswith('support') else 'unclear')
    grade = str(claim.get('evidence_grade') or '').lower()
    refs = [str(r) for r in (claim.get('entity_refs') or [])]
    is_contested = any(r.lower() in contested for r in refs)

    badges = [_badge(f'confidence: {conf}', f'conf-{conf}'),
              _badge(stance_key, f'stance-{stance_key}')]
    if grade in _GRADE_LABELS:
        badges.append(_badge(_GRADE_LABELS[grade], f'grade-{grade}'))
    if is_contested:
        badges.append(_badge('contested entity', 'grade-contested'))

    sid = claim.get('source_id')
    cite = ''
    if sid in src_num:
        n = src_num[sid]
        cite = f'<a class="claim-cite" href="#src-{n}">[{n}]</a>'
    ref_str = ''
    if refs:
        ref_str = ('<div class="claim-refs">'
                   + ' '.join(f'<span class="tag">{html.escape(r)}</span>' for r in refs[:6])
                   + '</div>')
    claim_text = _apply_cite_tokens(_inline(claim.get("text") or ""), src_num)
    return (
        f'<div class="claim claim-{stance_key}">'
        f'<div class="claim-text">{claim_text} {cite}</div>'
        f'<div class="claim-badges">{"".join(badges)}</div>'
        f'{ref_str}</div>'
    )


def _scorecard(checkpoints: list[dict]) -> str:
    if not checkpoints:
        return ''
    last = checkpoints[-1]
    metrics = last.get('metrics') or {}
    score = last.get('score')
    if not metrics and score is None and not last.get('critique_md'):
        return ''
    rows = ''
    for k in ('relevance', 'citation_quality', 'actionability', 'novelty', 'user_fit'):
        if k in metrics:
            v = metrics[k]
            pct = ''
            try:
                pct = f'<div class="meter"><div class="meter-fill" style="width:{float(v) * 10 if float(v) <= 10 else float(v)}%"></div></div>'
            except Exception:
                pass
            rows += f'<tr><td>{k.replace("_", " ")}</td><td class="score">{html.escape(str(v))}</td><td>{pct}</td></tr>'
    crit = last.get('critique_md') or ''
    crit_html = f'<details class="critique"><summary>Judge critique</summary>{markdown_to_html(crit)}</details>' if crit else ''
    score_line = f'<div class="score-overall">Overall judge score: <strong>{html.escape(str(score))}</strong></div>' if score is not None else ''
    table = f'<table class="scorecard">{rows}</table>' if rows else ''
    return f'<div class="judge">{score_line}{table}{crit_html}</div>'


CSS = """
:root{--bg:#fbfbfa;--fg:#1a1a1a;--muted:#6b6b6b;--card:#fff;--border:#e6e4df;
--accent:#5b4bd6;--accent-soft:#efedff;--good:#1a7f5a;--warn:#b26a00;--bad:#b3261e;
--good-soft:#e4f4ec;--warn-soft:#fbeede;--bad-soft:#fbe6e4;--radius:12px;
--maxw:920px;--mono:ui-monospace,SFMono-Regular,Menlo,monospace;
--sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#e9e9ec;--muted:#9a9aa2;
--card:#1e1e24;--border:#2e2e37;--accent:#a99bff;--accent-soft:#26243a;
--good:#4bd39a;--warn:#e0a44a;--bad:#ff6b5e;--good-soft:#16302447;--warn-soft:#3a2c1447;--bad-soft:#3a1c1a47}}
:root[data-theme=dark]{--bg:#16161a;--fg:#e9e9ec;--muted:#9a9aa2;--card:#1e1e24;
--border:#2e2e37;--accent:#a99bff;--accent-soft:#26243a;--good:#4bd39a;--warn:#e0a44a;
--bad:#ff6b5e;--good-soft:#16302447;--warn-soft:#3a2c1447;--bad-soft:#3a1c1a47}
:root[data-theme=light]{--bg:#fbfbfa;--fg:#1a1a1a;--muted:#6b6b6b;--card:#fff;
--border:#e6e4df;--accent:#5b4bd6;--accent-soft:#efedff;--good:#1a7f5a;--warn:#b26a00;
--bad:#b3261e;--good-soft:#e4f4ec;--warn-soft:#fbeede;--bad-soft:#fbe6e4}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);
line-height:1.6;font-size:16px}
.wrap{max-width:var(--maxw);margin:0 auto;padding:32px 20px 96px}
header.report-head{border-bottom:1px solid var(--border);padding-bottom:24px;margin-bottom:8px}
.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);font-weight:600}
h1{font-size:2rem;line-height:1.2;margin:.3em 0}
.question{font-size:1.1rem;color:var(--muted);margin:.5em 0 1em}
.meta-row{display:flex;flex-wrap:wrap;gap:8px 16px;font-size:13px;color:var(--muted)}
.meta-row b{color:var(--fg)}
h2{font-size:1.5rem;margin:2em 0 .5em;padding-top:.3em}
h3{font-size:1.18rem;margin:1.6em 0 .4em}
h4{font-size:1.02rem;margin:1.3em 0 .3em}
p{margin:.7em 0}a{color:var(--accent)}
code{font-family:var(--mono);background:var(--accent-soft);padding:.1em .35em;border-radius:5px;font-size:.9em}
ul,ol{padding-left:1.4em}li{margin:.25em 0}
blockquote{border-left:3px solid var(--accent);margin:.8em 0;padding:.2em 1em;color:var(--muted)}
hr{border:none;border-top:1px solid var(--border);margin:2em 0}
.badge{display:inline-block;font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;
border:1px solid var(--border);white-space:nowrap;line-height:1.5}
.badge-conf-high{background:var(--good-soft);color:var(--good);border-color:transparent}
.badge-conf-medium{background:var(--warn-soft);color:var(--warn);border-color:transparent}
.badge-conf-low,.badge-conf-unknown{background:var(--bad-soft);color:var(--bad);border-color:transparent}
.badge-stance-supports{background:var(--good-soft);color:var(--good);border-color:transparent}
.badge-stance-contradicts{background:var(--bad-soft);color:var(--bad);border-color:transparent}
.badge-stance-unclear{background:var(--accent-soft);color:var(--accent);border-color:transparent}
.badge-grade-strong{background:var(--good-soft);color:var(--good);border-color:transparent}
.badge-grade-moderate{background:var(--warn-soft);color:var(--warn);border-color:transparent}
.badge-grade-weak,.badge-grade-anecdotal{background:var(--bad-soft);color:var(--bad);border-color:transparent}
.badge-grade-contested{background:var(--bad-soft);color:var(--bad);border-color:transparent;font-weight:700}
.badge-grade-theoretical{background:var(--accent-soft);color:var(--accent);border-color:transparent}
.badge-verified{background:var(--good-soft);color:var(--good);border-color:transparent}
.badge-unverified{background:var(--bad-soft);color:var(--bad);border-color:transparent}
.badge-type,.badge-cred-high,.badge-cred-medium,.badge-cred-low,.badge-cred-unknown{color:var(--muted)}
.legend{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
padding:16px 20px;margin:20px 0;font-size:13px}
.legend h4{margin:0 0 10px}.legend-grid{display:flex;flex-wrap:wrap;gap:8px 20px}
.legend-item{display:flex;align-items:center;gap:6px}
.epi-tiles{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}
.tile{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
padding:12px 18px;min-width:110px}
.tile .n{font-size:1.6rem;font-weight:700;line-height:1}
.tile .l{font-size:12px;color:var(--muted);margin-top:4px}
.topic{margin:2.5em 0;border-top:2px solid var(--border);padding-top:1em}
.claims-wrap{margin:1.5em 0}
.claim{background:var(--card);border:1px solid var(--border);border-left:4px solid var(--muted);
border-radius:var(--radius);padding:14px 18px;margin:12px 0}
.claim-supports{border-left-color:var(--good)}
.claim-contradicts{border-left-color:var(--bad)}
.claim-unclear{border-left-color:var(--accent)}
.claim-text{font-size:.98rem}
.claim-badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.claim-cite{font-size:.8em;color:var(--accent);text-decoration:none;font-weight:600;vertical-align:super}
.cite-ref{font-size:.72em;font-weight:600;line-height:0}
.cite-ref a{color:var(--accent);text-decoration:none}
.cite-ref a:hover{text-decoration:underline}
.claim-refs{margin-top:8px}
.tag{display:inline-block;font-size:11px;color:var(--muted);background:var(--accent-soft);
padding:1px 7px;border-radius:5px;margin:2px 3px 0 0}
.cites{list-style:none;padding:0;margin:1em 0}
.cite{display:flex;gap:10px;padding:10px 0;border-bottom:1px solid var(--border)}
.cite-num{color:var(--muted);font-family:var(--mono);font-size:.85em;flex-shrink:0}
.cite-title{font-weight:600;font-size:.95rem}
.cite-authors,.cite-meta{font-size:.82rem;color:var(--muted)}
.judge{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px 20px;margin:1.5em 0}
.scorecard{width:100%;border-collapse:collapse;font-size:.9rem}
.scorecard td{padding:6px 8px;border-bottom:1px solid var(--border)}
.scorecard .score{font-weight:700;text-align:right;width:48px}
.meter{background:var(--border);border-radius:999px;height:7px;overflow:hidden;min-width:120px}
.meter-fill{background:var(--accent);height:100%}
.critique{margin-top:12px;font-size:.92rem}
.critique summary{cursor:pointer;font-weight:600;color:var(--accent)}
.theme-toggle{position:fixed;top:16px;right:16px;background:var(--card);border:1px solid var(--border);
color:var(--fg);border-radius:999px;width:38px;height:38px;cursor:pointer;font-size:16px}
.foot{margin-top:3em;padding-top:1.5em;border-top:1px solid var(--border);font-size:13px;color:var(--muted)}
details.report-src{margin:1em 0}details.report-src summary{cursor:pointer;color:var(--muted);font-size:.9em}
@media(max-width:600px){h1{font-size:1.5rem}.wrap{padding:20px 14px 72px}}
"""

TOGGLE_JS = """
(function(){var r=document.documentElement,k='libris-theme',s=localStorage.getItem(k);
if(s)r.setAttribute('data-theme',s);
document.getElementById('tt').addEventListener('click',function(){
var cur=r.getAttribute('data-theme')||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
var nx=cur==='dark'?'light':'dark';r.setAttribute('data-theme',nx);localStorage.setItem(k,nx);});})();
"""


def render_html(data: dict, *, title: str = '', verified: dict | None = None,
                subtitle: str = '') -> str:
    title = title or (data['topics'][0]['title'] if data['topics'] else 'Libris research report')
    epi = epistemic_summary(data['claims'])
    usage = data['usage'] or {}
    cost = usage.get('estimated_cost_usd')
    toks = usage.get('total_tokens')
    gen = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    meta_bits = [
        f'<span><b>{len(data["sources"])}</b> sources</span>',
        f'<span><b>{epi["total"]}</b> graded claims</span>',
        f'<span><b>{len(data["topics"])}</b> topic(s)</span>',
    ]
    if toks:
        meta_bits.append(f'<span><b>{int(toks):,}</b> tokens</span>')
    if cost:
        meta_bits.append(f'<span><b>${float(cost):.2f}</b></span>')
    meta_bits.append(f'<span>rendered {gen}</span>')

    tiles = (
        f'<div class="tile"><div class="n">{epi["confidence"]["high"]}</div><div class="l">high-confidence claims</div></div>'
        f'<div class="tile"><div class="n">{epi["confidence"]["medium"]}</div><div class="l">medium-confidence</div></div>'
        f'<div class="tile"><div class="n">{epi["confidence"]["low"]}</div><div class="l">low-confidence</div></div>'
        f'<div class="tile"><div class="n">{epi["stance"]["contradicts"]}</div><div class="l">contradicting claims</div></div>'
        f'<div class="tile"><div class="n">{len(epi["contested"])}</div><div class="l">contested entities</div></div>'
    )

    legend = (
        '<div class="legend"><h4>How to read this report</h4><div class="legend-grid">'
        '<span class="legend-item">' + _badge('confidence: high', 'conf-high') + ' well-supported</span>'
        '<span class="legend-item">' + _badge('confidence: medium', 'conf-medium') + ' plausible, partial support</span>'
        '<span class="legend-item">' + _badge('confidence: low', 'conf-low') + ' weak / single-source</span>'
        '<span class="legend-item">' + _badge('supports', 'stance-supports') + ' evidence for</span>'
        '<span class="legend-item">' + _badge('contradicts', 'stance-contradicts') + ' evidence against</span>'
        '<span class="legend-item">' + _badge('verified', 'verified') + ' citation resolved</span>'
        '</div><p style="margin:.7em 0 0;color:var(--muted)">Every claim traces to a numbered citation. '
        'Epistemic grades are assigned by the research agents and, where shown, checked by an independent judge.</p></div>'
    )

    sections = []
    for topic in data['topics']:
        src_num = {}
        cites_html = []
        for i, s in enumerate(topic['sources'], 1):
            src_num[s.get('source_id')] = i
            cites_html.append(_source_citation(s, i, verified))
        contested = set(epi['contested'])
        claim_cards = ''.join(_claim_card(c, src_num, contested) for c in topic['claims'])
        if topic['report_md']:
            report_html = _apply_cite_tokens(markdown_to_html(topic['report_md']), src_num)
        else:
            report_html = '<p><em>No report body was saved for this topic.</em></p>'
        why = f'<p class="question">{_inline(topic["why"])}</p>' if topic['why'] else ''
        fq = ''
        if topic['focus_questions']:
            fq = '<ul>' + ''.join(f'<li>{_inline(q)}</li>' for q in topic['focus_questions']) + '</ul>'
        claims_block = ''
        if claim_cards:
            claims_block = f'<h3>Graded claims</h3><div class="claims-wrap">{claim_cards}</div>'
        cites_block = ''
        if cites_html:
            cites_block = f'<h3>Citations</h3><ol class="cites">{"".join(cites_html)}</ol>'
        multi = len(data['topics']) > 1
        head = f'<h2>{html.escape(topic["title"])}</h2>' if multi else ''
        sections.append(
            f'<section class="topic">{head}{why}{fq}'
            f'{report_html}{_scorecard(topic["checkpoints"])}'
            f'{claims_block}{cites_block}</section>'
        )

    sub = f'<p class="question">{html.escape(subtitle)}</p>' if subtitle else (
        f'<p class="question">{html.escape(data["prompt"][:400])}</p>' if data['prompt'] else '')

    body = (
        '<button id="tt" class="theme-toggle" title="Toggle theme">◐</button>'
        '<div class="wrap">'
        '<header class="report-head">'
        '<div class="eyebrow">Libris · autonomous research</div>'
        f'<h1>{html.escape(title)}</h1>{sub}'
        f'<div class="meta-row">{"".join(meta_bits)}</div>'
        '</header>'
        f'<div class="epi-tiles">{tiles}</div>'
        f'{legend}'
        f'{"".join(sections)}'
        '<div class="foot">Generated by Libris, Charon\'s autonomous multi-agent research system. '
        'Claims and citations are produced by AI research agents and graded for confidence and stance; '
        'treat this as a well-sourced starting point, not a substitute for reading the primary literature. '
        'Citations marked “unverified” could not be resolved automatically and warrant manual checking.</div>'
        '</div>'
        f'<script>{TOGGLE_JS}</script>'
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{html.escape(title)}</title><style>{CSS}</style></head>'
        f'<body>{body}</body></html>'
    )


def render_operation(operation_dir: Path, *, title: str = '', verified: dict | None = None,
                     subtitle: str = '') -> str:
    data = load_operation(operation_dir)
    return render_html(data, title=title, verified=verified, subtitle=subtitle)


def main() -> int:
    ap = argparse.ArgumentParser(description='Render a Libris operation as a self-contained HTML report.')
    ap.add_argument('operation_dir')
    ap.add_argument('--out', default='')
    ap.add_argument('--title', default='')
    ap.add_argument('--subtitle', default='')
    args = ap.parse_args()

    op_dir = Path(args.operation_dir)
    html_str = render_operation(op_dir, title=args.title, subtitle=args.subtitle)
    out = Path(args.out) if args.out else op_dir / 'report.html'
    out.write_text(html_str, encoding='utf-8')
    print(f'wrote {out} ({len(html_str):,} bytes)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
