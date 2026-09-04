"""Office document text extraction for the Read tool.

Converts XLSX/XLSM, DOCX, and PPTX files to plain text so the agent can
read them through the normal Read pagination path. Extractors are pure
functions returning the full text; callers handle offset/limit/truncation.

Dependencies (openpyxl, python-docx, python-pptx) are imported lazily so
the tool registry never fails to import when they are missing.
"""
from __future__ import annotations

from pathlib import Path

OFFICE_SUFFIXES = {'.xlsx', '.xlsm', '.docx', '.pptx'}

# Legacy binary formats we deliberately don't parse.
LEGACY_SUFFIXES = {
    '.xls': 'xlsx (open in a spreadsheet app and re-save, or use `ssconvert`)',
    '.doc': 'docx (open in a word processor and re-save, or use `textutil -convert docx` on macOS)',
    '.ppt': 'pptx (open in a presentation app and re-save)',
}

MAX_ROWS_PER_SHEET = 1000
MAX_COLS_PER_ROW = 100


class OfficeExtractError(Exception):
    """Raised when an office document cannot be converted to text."""


def _missing_dep(lib: str, suffix: str) -> OfficeExtractError:
    return OfficeExtractError(
        f'{lib} not installed; required to read {suffix} files. '
        f'Install with: pip install {lib}'
    )


def _cell_to_str(value) -> str:
    if value is None:
        return ''
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def _extract_xlsx(path: Path) -> str:
    try:
        import openpyxl
    except ImportError:
        raise _missing_dep('openpyxl', path.suffix) from None

    # data_only=True yields cached formula results (what the user sees in Excel)
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    parts: list[str] = []
    try:
        for ws in wb.worksheets:
            parts.append(f'## Sheet: {ws.title}')
            row_count = 0
            for row in ws.iter_rows(values_only=True):
                if row_count >= MAX_ROWS_PER_SHEET:
                    parts.append(f'[... remaining rows omitted, sheet has more than {MAX_ROWS_PER_SHEET} rows]')
                    break
                cells = [_cell_to_str(v) for v in row[:MAX_COLS_PER_ROW]]
                # Skip fully empty rows but keep one marker so structure is visible
                if any(c for c in cells):
                    parts.append('\t'.join(cells).rstrip())
                    row_count += 1
            parts.append('')
    finally:
        wb.close()
    return '\n'.join(parts)


def _extract_docx(path: Path) -> str:
    try:
        import docx
    except ImportError:
        raise _missing_dep('python-docx', path.suffix) from None

    document = docx.Document(str(path))
    parts: list[str] = []
    for para in document.paragraphs:
        parts.append(para.text)
    for i, table in enumerate(document.tables):
        parts.append(f'## Table {i + 1}')
        for row in table.rows:
            parts.append('\t'.join(cell.text.strip() for cell in row.cells))
    return '\n'.join(parts)


def _extract_pptx(path: Path) -> str:
    try:
        import pptx
    except ImportError:
        raise _missing_dep('python-pptx', path.suffix) from None

    prs = pptx.Presentation(str(path))
    parts: list[str] = []
    for i, slide in enumerate(prs.slides):
        parts.append(f'## Slide {i + 1}')
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = ''.join(run.text for run in para.runs)
                    if text.strip():
                        parts.append(text)
            if getattr(shape, 'has_table', False) and shape.has_table:
                for row in shape.table.rows:
                    parts.append('\t'.join(cell.text.strip() for cell in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f'[Notes: {notes}]')
        parts.append('')
    return '\n'.join(parts)


def extract_office_text(path: Path) -> str:
    """Extract plain text from an office document. Raises OfficeExtractError."""
    suffix = path.suffix.lower()
    if suffix in LEGACY_SUFFIXES:
        raise OfficeExtractError(
            f'Legacy format {suffix} is not supported. Convert to {LEGACY_SUFFIXES[suffix]}.'
        )
    try:
        if suffix in ('.xlsx', '.xlsm'):
            return _extract_xlsx(path)
        if suffix == '.docx':
            return _extract_docx(path)
        if suffix == '.pptx':
            return _extract_pptx(path)
    except OfficeExtractError:
        raise
    except Exception as e:
        raise OfficeExtractError(f'Failed to extract {path.name}: {e}') from e
    raise OfficeExtractError(f'Unsupported office format: {suffix}')
