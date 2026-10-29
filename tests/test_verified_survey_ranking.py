import sqlite3
from utils.hybrid_discovery import (
    _published_reference_year,
    _rank_survey_editions,
    _survey_years_from_catalog,
)


def test_reference_year_only_from_survey_statement():
    assert _published_reference_year('Reference: General Household Survey 2025') == 2025
    assert _published_reference_year('Census of Population 2020') == 2020
    assert _published_reference_year('Updated 2026') is None


def test_reorders_only_same_title_with_verified_years():
    def row(tid, title, score):
        return {'table_id': tid, 'best': {'table_title': title}, 'score': score}

    entries = [
        row('17621', 'Resident Students by Mode', 0.052),
        row('C020182', 'Resident Students by Mode', 0.051),
        row('X1', 'Other topic', 0.050),
    ]
    years = {'17621': 2020, 'C020182': 2025}
    assert (
        _rank_survey_editions(entries.copy(), 'How many resident students?', years)[0]['table_id']
        == 'C020182'
    )
    assert (
        _rank_survey_editions(entries.copy(), 'How many in 2020?', years)[0]['table_id'] == '17621'
    )
    assert (
        _rank_survey_editions(entries.copy(), 'How many in 2025?', years)[0]['table_id']
        == 'C020182'
    )
    assert _rank_survey_editions(entries.copy(), 'How many?', {})[0]['table_id'] == '17621'


def test_catalog_uses_footnote_and_description():
    con = sqlite3.connect(':memory:')
    con.execute('CREATE TABLE kb_tables (table_id TEXT, description TEXT)')
    con.execute('CREATE TABLE kb_series (table_id TEXT, footnote TEXT)')
    con.executemany('INSERT INTO kb_tables VALUES (?,?)', [('17621', ''), ('C020182', '')])
    con.executemany(
        'INSERT INTO kb_series VALUES (?,?)',
        [
            ('17621', 'Reference: Census of Population 2020'),
            ('C020182', 'Reference: General Household Survey 2025'),
        ],
    )
    assert _survey_years_from_catalog(con, ['17621', 'C020182']) == {'17621': 2020, 'C020182': 2025}
