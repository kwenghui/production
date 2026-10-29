import sqlite3

from utils.official_links import table_page_url
from utils.hybrid_discovery import _same_title_survey_hits, _ranked_tables


def test_legacy_numeric_official_url():
    assert table_page_url('6418') == 'https://tablebuilder.singstat.gov.sg/api/table/tabledata/6418'
    assert table_page_url('C020162') == 'https://tablebuilder.singstat.gov.sg/table/CT/C020162'
    assert table_page_url('../6418') == ''


def test_same_title_survey_rescue_is_bounded_and_does_not_guess_year():
    con = sqlite3.connect(':memory:')
    con.row_factory = sqlite3.Row
    con.execute(
        'CREATE TABLE kb_tables (table_id TEXT, title TEXT, frequency TEXT, source_url TEXT, series_status TEXT)'
    )
    title = 'Resident Students by Travelling Time to School'
    for tid in ('6418', '17603', 'C020162'):
        con.execute(
            'INSERT INTO kb_tables VALUES (?, ?, ?, ?, ?)',
            (tid, title, '', table_page_url(tid), 'incomplete'),
        )
    hits = [
        {
            'table_id': '6418',
            'table_title': title,
            '_backend': 'SQLite FTS5',
            'source_url': table_page_url('6418'),
        }
    ]
    rescued = _same_title_survey_hits(con, hits)
    assert {x['table_id'] for x in rescued} == {'17603', 'C020162'}
    assert all(x['source_url'].startswith('https://tablebuilder.singstat.gov.sg/') for x in rescued)
    ranked = _ranked_tables(
        hits + rescued, question='Resident Students by Travelling Time to School', limit=5
    )
    assert {x['table_id'] for x in ranked} == {'6418', '17603', 'C020162'}
