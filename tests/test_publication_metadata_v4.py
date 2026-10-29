from utils import hybrid_discovery as hd


def test_reference_year_and_update_date_are_separate():
    def payload(tid, kind, updated):
        return {'Data': {'records': {'id': tid, 'tableType': kind, 'dataLastUpdated': updated}}}

    older = hd._official_publication_metadata(
        payload('17621', 'Census of Population 2020', '30/06/2026'), '17621'
    )
    newer = hd._official_publication_metadata(
        payload('C020182', 'General Household Survey 2025', '01/01/2025'), 'C020182'
    )
    assert older['reference_year'] == 2020
    assert newer['reference_year'] == 2025
    assert older['data_last_updated'] == '30/06/2026'
    assert older['reference_source'] == 'Data.records.tableType'
    assert newer['reference_year'] > older['reference_year']
    assert (
        hd._official_publication_metadata(payload('17621', '', '30/06/2026'), '17621')[
            'reference_year'
        ]
        is None
    )
    assert (
        hd._official_publication_metadata(
            payload('17621', 'Census of Population 2020', ''), 'WRONG'
        )
        == {}
    )
