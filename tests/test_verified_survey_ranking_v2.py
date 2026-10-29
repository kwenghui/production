from utils.hybrid_discovery import (
    _published_year_in_official_payload,
    _complete_edition_years,
    _rank_survey_editions,
)


def test_official_metadata_year():
    payload = {
        'Data': {
            'records': {'id': 'C020182', 'description': 'Reference: General Household Survey 2025'}
        }
    }
    assert _published_year_in_official_payload(payload, 'C020182') == 2025
    assert _published_year_in_official_payload(payload, '17621') is None
    assert (
        _published_year_in_official_payload(
            {'Data': {'records': {'id': 'C020182', 'description': 'Updated 2026'}}}, 'C020182'
        )
        is None
    )


def test_enrichment_only_for_duplicate_titles(monkeypatch):
    from utils import hybrid_discovery as hd

    called = []

    def fake(tid):
        called.append(tid)
        return {'17621': 2020, 'C020182': 2025}.get(tid)

    monkeypatch.setattr(hd, '_official_reference_year', fake)
    rows = [
        {'table_id': '17621', 'score': 0.052, 'best': {'table_title': 'Same title'}},
        {'table_id': 'C020182', 'score': 0.051, 'best': {'table_title': 'Same title'}},
        {'table_id': 'OTHER', 'score': 0.050, 'best': {'table_title': 'Different title'}},
    ]
    years = _complete_edition_years(rows, {})
    assert called == ['17621', 'C020182']
    assert _rank_survey_editions(rows, 'How many?', years)[0]['table_id'] == 'C020182'
