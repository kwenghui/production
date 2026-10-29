"""Regression tests using the actual SingStat metadata response shape."""

from utils import hybrid_discovery as hd


def payload(tid, table_type, updated='30/06/2026'):
    return {
        'Data': {
            'records': {
                'id': tid,
                'tableType': table_type,
                'dataLastUpdated': updated,
                'title': 'Resident Students Aged 5 Years and Over by Usual Mode of Transport to School, Difficulty in Performing Basic Activities and Age Group',
            }
        }
    }


def test_actual_singstat_metadata_shape():
    assert (
        hd._published_year_in_official_payload(
            payload('17621', 'Census of Population 2020'), '17621'
        )
        == 2020
    )
    assert (
        hd._published_year_in_official_payload(
            payload('C020182', 'General Household Survey 2025'), 'C020182'
        )
        == 2025
    )
    assert (
        hd._published_year_in_official_payload(
            payload('C020182', 'General Household Survey 2025'), '17621'
        )
        is None
    )
    assert hd._published_year_in_official_payload(payload('17621', 'Unspecified'), '17621') is None


def test_current_and_historical_ordering():
    title = 'Resident Students Aged 5 Years and Over by Usual Mode of Transport to School, Difficulty in Performing Basic Activities and Age Group'
    rows = [
        {'table_id': '17621', 'score': 0.0522732, 'best': {'table_title': title}},
        {'table_id': 'C020182', 'score': 0.051, 'best': {'table_title': title}},
        {'table_id': '17598', 'score': 0.0434, 'best': {'table_title': 'Different subject'}},
    ]
    years = {'17621': 2020, 'C020182': 2025}
    assert (
        hd._rank_survey_editions(
            [dict(x) for x in rows], 'How many students travel to school?', years
        )[0]['table_id']
        == 'C020182'
    )
    assert (
        hd._rank_survey_editions([dict(x) for x in rows], 'How many students in 2020?', years)[0][
            'table_id'
        ]
        == '17621'
    )
    assert (
        hd._rank_survey_editions([dict(x) for x in rows], 'How many students in 2025?', years)[0][
            'table_id'
        ]
        == 'C020182'
    )
    assert (
        hd._rank_survey_editions([dict(x) for x in rows], 'How many students?', {})[0]['table_id']
        == '17621'
    )
