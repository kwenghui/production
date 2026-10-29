"""Additive, fail-closed SingStat structured ingestion and source suitability.

Does not alter kb_observations, ranking, or authorize calculations.

Used by: utils/kb.py.
Tested in: tests/test_discovery_and_ingest.py.
"""

from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from utils.singstat_api import request_json, safe_id

YEAR = re.compile(r'(?<!\d)(?:18|19|20)\d{2}(?!\d)')
PERIOD = re.compile(
    r'^(?:18|19|20)\d{2}(?:[- ]?(?:Q[1-4]|M(?:0?[1-9]|1[0-2])|0?[1-9]|1[0-2]))?$', re.I
)
SURVEY = re.compile(r'\b(?:census|survey)\b', re.I)


def reference_info(meta) -> dict:
    """Only infer a year from explicit survey/census title/type; never update date."""
    typ = str(meta.get('tableType') or '')
    title = str(meta.get('title') or '')
    years = YEAR.findall(typ + ' ' + title) if SURVEY.search(typ + ' ' + title) else []
    return {
        'reference_year': int(years[-1]) if years and len(set(years)) == 1 else None,
        'reference_basis': 'survey_or_census_label'
        if years and len(set(years)) == 1
        else 'unknown',
        'frequency': str(meta.get('frequency') or ''),
        'table_type': typ,
        'source_last_updated': str(meta.get('dataLastUpdated') or ''),
        'api_generated_at': str(meta.get('dateGenerated') or ''),
    }


def source_suitability(meta, requested_year=None) -> dict:
    """Whether a table's reference year and frequency suit the requested year."""
    info = reference_info(meta)
    freq = info['frequency'].strip().lower()
    year = info['reference_year']
    if requested_year is not None and year is not None and int(requested_year) != year:
        decision = 'reference_year_mismatch'
    elif requested_year is None and year is not None:
        decision = 'historical_snapshot'
    elif freq in ('annual', 'yearly'):
        decision = 'annual_candidate_needs_observation_period'
    else:
        decision = 'period_unresolved'
    return {**info, 'suitability': decision, 'answer_ready': False}


def leaves(nodes, prefix=(), depth: int = 0, *, missing=None):
    """Yield (dimension path, raw value) for every leaf of nested official columns."""
    if depth > 12:
        raise ValueError('Excessive nested dimensions')
    if not isinstance(nodes, list):
        raise ValueError('columns must be a list')
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError('column must be an object')
        key = str(node.get('key') or '').strip()
        if not key:
            raise ValueError('column missing key')
        path = prefix + (key,)
        if 'columns' in node:
            if 'value' in node:
                raise ValueError('Ambiguous column has children and value')
            yield from leaves(node['columns'], path, depth + 1, missing=missing)
        elif 'value' in node:
            yield path, node['value']
        else:
            if missing is not None:
                missing.append(path)
                continue
            raise ValueError(
                f'column has neither value nor children at path {path!r}; '
                f'keys={sorted(node.keys())!r}'
            )


def numeric(raw):
    """The cell as an exact decimal string, or None if it is not a plain number."""
    s = str(raw if raw is not None else '').strip().replace(',', '')
    if not re.fullmatch(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)', s):
        return None
    try:
        return str(Decimal(s))
    except InvalidOperation:
        return None


def classify(meta, paths) -> str:
    """Do not infer a period from category names such as Total."""
    if paths and all(len(p) == 1 and PERIOD.fullmatch(p[0]) for p in paths):
        return 'time_series'
    return 'cross_sectional'


def init_schema(con) -> None:
    """Create the structured-ingestion tables if they do not exist."""
    con.executescript('''
    CREATE TABLE IF NOT EXISTS kb_structured_sources (
      table_id TEXT PRIMARY KEY, title TEXT NOT NULL, table_type TEXT NOT NULL,
      frequency TEXT NOT NULL, reference_year INTEGER, reference_basis TEXT NOT NULL,
      source_last_updated TEXT NOT NULL, api_generated_at TEXT NOT NULL,
      fetched_at TEXT NOT NULL, ingestion_status TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS kb_structured_values (
      table_id TEXT NOT NULL, series_id TEXT NOT NULL, series_title TEXT NOT NULL,
      unit TEXT NOT NULL, shape TEXT NOT NULL, dimension_path TEXT NOT NULL,
      reference_period TEXT, raw_value TEXT NOT NULL, numeric_value TEXT,
      fetched_at TEXT NOT NULL,
      PRIMARY KEY(table_id, series_id, dimension_path),
      FOREIGN KEY(table_id) REFERENCES kb_structured_sources(table_id)
    );
    CREATE TABLE IF NOT EXISTS kb_structured_missing_cells (
      table_id TEXT NOT NULL, series_id TEXT NOT NULL,
      dimension_path TEXT NOT NULL, reason TEXT NOT NULL,
      fetched_at TEXT NOT NULL,
      PRIMARY KEY(table_id,series_id,dimension_path)
    );
    CREATE INDEX IF NOT EXISTS idx_kb_structured_missing_lookup
      ON kb_structured_missing_cells(table_id,series_id);
    CREATE INDEX IF NOT EXISTS idx_kb_structured_values_lookup
      ON kb_structured_values(table_id,series_id,reference_period);
    ''')


def ingest_payload(con, payload, expected_id, *, complete_response: bool = False) -> dict:
    """Atomic import of a complete response; never writes legacy observation rows."""
    data = payload.get('Data')
    if not isinstance(data, dict) or str(data.get('id', '')).upper() != safe_id(expected_id):
        raise ValueError('Official table identity mismatch')
    rows = data.get('row')
    if not isinstance(rows, list):
        raise ValueError('Missing series rows')
    if not complete_response:
        raise ValueError('Cannot certify completeness of paginated source')
    info = reference_info(data)
    fetched = datetime.now(timezone.utc).isoformat(timespec='seconds')
    parsed = []
    seen = set()
    missing_paths = []
    missing_cells = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid series row')
        sid = str(row.get('seriesNo') or row.get('rowNo') or '').strip()
        if not sid:
            raise ValueError('Missing series identifier')
        row_missing = []
        values = list(leaves(row.get('columns') or [], missing=row_missing))
        for path in row_missing:
            path_json = json.dumps(path, ensure_ascii=False, separators=(',', ':'))
            if (sid, path_json) in seen:
                raise ValueError('Duplicate dimension path')
            seen.add((sid, path_json))
            missing_paths.append(path)
            missing_cells.append(
                (safe_id(expected_id), sid, path_json, 'key_only_no_value_or_children', fetched)
            )
        shape = classify(data, [path for path, _ in values])
        for path, raw in values:
            path_json = json.dumps(path, ensure_ascii=False, separators=(',', ':'))
            if (sid, path_json) in seen:
                raise ValueError('Duplicate dimension path')
            seen.add((sid, path_json))
            period = path[0] if shape == 'time_series' else None
            parsed.append(
                (
                    safe_id(expected_id),
                    sid,
                    str(row.get('rowText') or ''),
                    str(row.get('uoM') or ''),
                    shape,
                    path_json,
                    period,
                    str(raw if raw is not None else ''),
                    numeric(raw),
                    fetched,
                )
            )
    with con:
        init_schema(con)
        con.execute(
            '''INSERT INTO kb_structured_sources VALUES(?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(table_id) DO UPDATE SET title=excluded.title,table_type=excluded.table_type,
          frequency=excluded.frequency,reference_year=excluded.reference_year,
          reference_basis=excluded.reference_basis,source_last_updated=excluded.source_last_updated,
          api_generated_at=excluded.api_generated_at,fetched_at=excluded.fetched_at,
          ingestion_status=excluded.ingestion_status''',
            (
                safe_id(expected_id),
                str(data.get('title') or ''),
                info['table_type'],
                info['frequency'],
                info['reference_year'],
                info['reference_basis'],
                info['source_last_updated'],
                info['api_generated_at'],
                fetched,
                'partial_key_only_dimensions' if missing_paths else 'complete_response',
            ),
        )
        con.execute('DELETE FROM kb_structured_values WHERE table_id=?', (safe_id(expected_id),))
        con.execute(
            'DELETE FROM kb_structured_missing_cells WHERE table_id=?', (safe_id(expected_id),)
        )
        con.executemany('INSERT INTO kb_structured_values VALUES(?,?,?,?,?,?,?,?,?,?)', parsed)
        con.executemany('INSERT INTO kb_structured_missing_cells VALUES(?,?,?,?,?)', missing_cells)
    return {
        'table_id': safe_id(expected_id),
        'series': len(rows),
        'values': len(parsed),
        'numeric_values': sum(v[8] is not None for v in parsed),
        'missing_key_only_paths': len(missing_paths),
        'missing_cells_persisted': len(missing_cells),
        'data_status': 'partial_missing_cells' if missing_cells else 'complete',
        'structure_complete': not bool(missing_paths),
        'shape_counts': {
            s: sum(v[4] == s for v in parsed) for s in ('time_series', 'cross_sectional')
        },
        **source_suitability(data),
    }


def fetch_complete(table_id, *, page_size: int = 250, max_pages: int = 200, fetch=None) -> dict:
    """Retrieve row-offset pages; certify completion only on a short/empty row page.

    Column leaves are observations *within* rows, not pagination units.
    Key-only nodes remain errors until their meaning is established.
    """
    if fetch is None:
        fetch = request_json
    tid = safe_id(table_id)
    if not 1 <= page_size <= 5000 or not 1 <= max_pages <= 1000:
        raise ValueError('Invalid pagination bounds')
    meta = None
    result = []
    seen_series = set()
    missing_paths = []
    for page in range(max_pages):
        offset = page * page_size
        obj = fetch('/tabledata/' + tid, {'limit': page_size, 'offset': offset})
        data = obj.get('Data') if isinstance(obj, dict) else None
        if not isinstance(data, dict) or str(data.get('id', '')).upper() != tid:
            raise ValueError('API table identity mismatch')
        rows = data.get('row')
        if not isinstance(rows, list):
            raise ValueError('Unexpected series rows')
        for field, expected in (('offset', offset), ('limit', page_size)):
            if field in data and str(data[field]) != str(expected):
                raise ValueError(f'API {field} does not match request')
        if meta is None:
            meta = {k: v for k, v in data.items() if k != 'row'}
        elif any(
            data.get(k) != meta.get(k)
            for k in ('title', 'tableType', 'frequency', 'dataLastUpdated', 'dataSource', 'id')
        ):
            raise ValueError('Table metadata changed during pagination')
        if len(rows) > page_size:
            raise ValueError('API returned more series rows than requested')
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Invalid series row')
            sid = str(row.get('seriesNo') or row.get('rowNo') or '').strip()
            if not sid or sid in seen_series:
                raise ValueError('Missing or repeated series identifier across pages')
            seen_series.add(sid)
            cols = row.get('columns')
            if not isinstance(cols, list):
                raise ValueError('Invalid series columns')
            # Validate all nested paths, even when no numeric observations exist.
            paths = [path for path, _ in leaves(cols, missing=missing_paths)]
            if len(paths) != len(set(paths)):
                raise ValueError('Duplicate dimension path within series')
            result.append(row)
        if len(rows) < page_size:
            return {
                'Data': {**meta, 'row': result},
                'structure_diagnostics': {
                    'key_only_paths': len(missing_paths),
                    'structure_complete': not bool(missing_paths),
                },
            }
    raise ValueError('Pagination limit reached; source completeness unknown')


def fetch_and_ingest(con, table_id, *, fetch=request_json):
    """Download one complete table and store its cells (all-or-nothing)."""
    payload = fetch_complete(table_id, fetch=fetch)
    return ingest_payload(con, payload, table_id, complete_response=True)
