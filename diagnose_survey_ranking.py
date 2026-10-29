"""Read-only diagnosis of Sage survey-year ranking. Run from Sage project root."""
import inspect
import json
import sqlite3
from pathlib import Path
from utils import hybrid_discovery as hd
from utils import singstat_api

Q = 'How many Singapore resident students aged 5 and over are there, broken down by how they usually travel to school, difficulty doing everyday activities and age group?'
IDS = ('17621', 'C020182')
print('LOADED MODULE:', Path(inspect.getfile(hd)).resolve())
print('HAS v2 metadata enrichment:', hasattr(hd, '_complete_edition_years'))
print('HAS v2 ranking:', hasattr(hd, '_rank_survey_editions'))
print('SQLite:', hd._path('SAGE_KB_SQLITE', 'data/knowledge_base.sqlite3'))
path = hd._path('SAGE_KB_SQLITE', 'data/knowledge_base.sqlite3')
con = sqlite3.connect(f'file:{Path(path).resolve().as_posix()}?mode=ro', uri=True)
try:
    for table in ('kb_tables', 'kb_series'):
        print('\nSCHEMA', table)
        columns = [r[1] for r in con.execute(f'PRAGMA table_info({table})')]
        print('columns:', columns)
        fields = [x for x in ('table_id','title','description','footnote','notes','source') if x in columns]
        if 'table_id' not in fields:
            continue
        rows = con.execute(f"SELECT {','.join(fields)} FROM {table} WHERE table_id IN (?, ?) LIMIT 8", IDS).fetchall()
        for row in rows:
            print({k: str(v)[:600] if v is not None else None for k,v in zip(fields,row)})
    if hasattr(hd,'_survey_years_from_catalog'):
        print('\nYEARS FROM LOCAL CATALOG:', hd._survey_years_from_catalog(con, IDS))
finally:
    con.close()
print('\nOFFICIAL API METADATA')
for tid in IDS:
    try:
        payload = singstat_api.request_json('/metadata/' + tid, timeout=10, tries=1)
        print(tid, 'top-level keys:', list(payload)[:12], 'Data type:', type(payload.get('Data')).__name__)
        data = payload.get('Data')
        print(tid, 'Data keys:', list(data)[:20] if isinstance(data, dict) else 'not a dictionary')
        if hasattr(hd,'_published_year_in_official_payload'):
            print(tid, 'extracted year:', hd._published_year_in_official_payload(payload, tid))
        print(tid, 'metadata preview:', json.dumps(data, ensure_ascii=False, default=str)[:900])
    except Exception as exc:
        print(tid, 'API ERROR:', type(exc).__name__, str(exc)[:350])
print('\nACTUAL RETRIEVAL (may use OpenAI embeddings)')
try:
    results = hd.retrieve(Q, top_k=5)
    for item in results:
        print(item.get('candidate_rank'), item.get('table_id'), item.get('score'), item.get('discovery_trace',{}).get('ranking',{}).get('published_survey_years'))
except Exception as exc:
    print('RETRIEVAL ERROR:', type(exc).__name__, str(exc)[:350])
