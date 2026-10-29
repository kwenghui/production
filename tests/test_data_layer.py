"""Tests for the data layer: the internet door, payload parsing, provenance, model evidence.

These modules sit between Sage and the official data source, so their rules
are security and correctness rules:

* utils/official_source.py      - only official HTTPS hosts; size limits; offline mode
* utils/singstat_generic_normalization.py - official JSON -> typed rows
* utils/retrieval_trace.py      - which official URLs were really fetched (no secrets)
* utils/model_evidence.py       - what the model is shown (bounded, no chart payloads)

No network is used: HTTP sessions are replaced by in-memory fakes.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import utils.official_source as official  # noqa: E402
from utils import retrieval_trace  # noqa: E402
from utils.model_evidence import compact_tool_result_for_model  # noqa: E402
from utils.periods import year_from_period  # noqa: E402
from utils.singstat_generic_normalization import normalize_generic_table  # noqa: E402

TABLE_URL = "https://tablebuilder.singstat.gov.sg/api/table/tabledata/M810001?limit=2500&offset=0"


class FakeResponse:
    """A streamed HTTP response with a status, headers and a body."""

    def __init__(self, body=b"{}", status=200, headers=None):
        self.body, self.status_code, self.headers = body, status, headers or {}
        self.url, self.closed = TABLE_URL, False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise official.requests.HTTPError(str(self.status_code), response=self)

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    """Records the request and returns a prepared response."""

    def __init__(self, response):
        self.response, self.calls = response, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class TheOnlyDoorToTheInternet(unittest.TestCase):
    """official_source._live_http_get and safe_http_get refuse anything but official HTTPS."""

    def test_non_official_or_malformed_urls_are_refused_before_any_request(self):
        session = FakeSession(FakeResponse())
        with mock.patch.object(official, "_http_session", return_value=session):
            for url in (
                "http://tablebuilder.singstat.gov.sg/api/x",  # not HTTPS
                "https://evil.example.com/api/table/tabledata/M1",  # other host
                "https://user:pw@tablebuilder.singstat.gov.sg/x",  # credentials
                "https://tablebuilder.singstat.gov.sg/x#frag",
            ):  # fragment
                with self.subTest(url=url):
                    with self.assertRaises(ValueError):
                        official._live_http_get(url)
                    with self.assertRaises(ValueError):
                        official.safe_http_get(url)
        self.assertEqual(session.calls, [])

    def test_required_headers_are_always_sent_and_redirects_refused(self):
        session = FakeSession(FakeResponse(b'{"Data": {}}'))
        with mock.patch.object(official, "_http_session", return_value=session):
            official._live_http_get(TABLE_URL, headers={"User-Agent": "  "})
        _, kwargs = session.calls[0]
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["headers"]["User-Agent"].strip())
        self.assertIn("Accept", kwargs["headers"])

    def test_oversized_responses_are_refused(self):
        too_big = FakeResponse(
            headers={"Content-Length": str(official.MAX_HTTP_RESPONSE_BYTES + 1)}
        )
        with mock.patch.object(official, "_http_session", return_value=FakeSession(too_big)):
            with self.assertRaises(ValueError):
                official._live_http_get(TABLE_URL)
        self.assertTrue(too_big.closed)
        streamed = FakeResponse(body=b"x" * (official.MAX_HTTP_RESPONSE_BYTES + 10))
        with mock.patch.object(official, "_http_session", return_value=FakeSession(streamed)):
            with self.assertRaises(ValueError):
                official._live_http_get(TABLE_URL)

    def test_offline_mode_without_the_store_never_goes_online(self):
        session = FakeSession(FakeResponse())
        with (
            mock.patch.multiple(official, SAGE_STORE_ENABLED=False, SAGE_OFFLINE_ONLY=True),
            mock.patch.object(official, "_http_session", return_value=session),
        ):
            with self.assertRaises(ValueError):
                official.safe_http_get(TABLE_URL)
        self.assertEqual(session.calls, [])


class PayloadNormalisation(unittest.TestCase):
    """Official table JSON becomes one typed row per (series, period)."""

    def setUp(self):
        self.payload = {
            "title": "Total Population, Annual",
            "frequency": "Annual",
            "row": [
                {
                    "seriesNo": "1",
                    "rowText": "Total",
                    "uoM": "Number",
                    "columns": [
                        {"key": "2023", "value": "5,917,600"},
                        {"key": "2024", "value": "na"},
                    ],
                },
                {
                    "seriesNo": "2",
                    "rowText": "",
                    "uoM": "Number",
                    "columns": [{"key": "2024", "value": "1"}],
                },
            ],
        }

    def normalise(self, payload):
        def coerce(raw):
            try:
                return float(str(raw).replace(",", ""))
            except ValueError:
                return None

        return normalize_generic_table(
            "M810001",
            payload,
            year_from_period=year_from_period,
            coerce_value=coerce,
            source_url=lambda t: f"https://example.test/{t}",
        )

    def test_rows_values_and_metadata(self):
        frame, metadata = self.normalise(self.payload)
        self.assertEqual(len(frame), 3)
        first = frame.iloc[0]
        self.assertEqual(
            (first["series_no"], first["period"], first["year"], first["value"]),
            ("1", "2023", 2023, 5917600.0),
        )
        self.assertNotEqual(frame.iloc[1]["value"], frame.iloc[1]["value"])  # "na" -> NaN, never 0
        self.assertEqual(frame.iloc[2]["series_name"], "Series 2")  # blank names get a label
        self.assertEqual(metadata["title"], "Total Population, Annual")
        self.assertTrue(metadata["table_complete"])

    def test_a_single_staged_series_is_never_called_complete(self):
        _, metadata = self.normalise({**self.payload, "staged_single_series": True})
        self.assertFalse(metadata["table_complete"])

    def test_an_empty_table_is_an_error_not_an_empty_answer(self):
        with self.assertRaises(ValueError):
            self.normalise({"row": []})


class RetrievalProvenance(unittest.TestCase):
    """Only real official tabledata URLs are recorded, stripped of anything sensitive."""

    def test_only_official_tabledata_urls_survive_and_extra_query_is_dropped(self):
        safe = retrieval_trace._safe_url(TABLE_URL + "&apikey=SECRET")
        self.assertEqual(safe, TABLE_URL)
        for url in (
            "https://evil.example.com/api/table/tabledata/M810001",
            "http://tablebuilder.singstat.gov.sg/api/table/tabledata/M810001",
            "https://u:p@tablebuilder.singstat.gov.sg/api/table/tabledata/M810001",
            "https://tablebuilder.singstat.gov.sg/api/metadata/M810001",
        ):
            with self.subTest(url=url):
                self.assertIsNone(retrieval_trace._safe_url(url))

    def test_a_fresh_download_is_reported_with_its_url(self):
        token = retrieval_trace.begin()
        try:
            retrieval_trace.record("M810001", "fresh", url=TABLE_URL)
            summary = retrieval_trace.summary()
        finally:
            retrieval_trace.end(token)
        self.assertEqual(summary.get("cache_status"), "fresh")
        self.assertEqual(summary.get("retrieval_api_urls"), [TABLE_URL])


class EvidenceShownToTheModel(unittest.TestCase):
    """The model sees bounded JSON without chart payloads."""

    def test_chart_data_is_withheld_but_its_existence_is_stated(self):
        result = {
            "success": True,
            "table_id": "M810001",
            "series_summary": [{"latest_value": 1.0}],
            "chart_spec": {"kind": "trend", "series": [{"values": [1, 2]}]},
        }
        payload = json.loads(compact_tool_result_for_model(result))
        self.assertNotIn("chart_spec", payload)
        self.assertTrue(payload["verified_chart_available"])
        self.assertEqual(payload["chart_format"], "trend")

    def test_oversized_evidence_is_never_cut_mid_value(self):
        result = {
            "success": True,
            "table_id": "M810001",
            "observations": [{"period": str(1900 + i), "value": i} for i in range(5000)],
        }
        text = compact_tool_result_for_model(result, max_chars=4000)
        self.assertLessEqual(len(text), 4000)
        json.loads(text)  # always valid JSON, never a truncated string


if __name__ == "__main__":
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-" + "a" * 40)
    unittest.main()
