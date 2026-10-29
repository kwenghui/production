"""Three real Sage answers (26 Oct 2026), as fixtures for answer-checking code.

Captured from "Show technical details" for these questions:

  A. "What is the make-up of nominal gross value added in Singapore, by
     industry, and how has it changed over the years?"           (M015781)
  B. "How have merchandise imports of machinery and equipment in Singapore
     changed month by month?"                                     (M451111)
  C. "How has research and development expenditure in Singapore changed over
     the years, by institutional sector and area of research?"    (M081331)

The official observations below are copied from those tool results. The
tool-result dicts are rebuilt in the same shape Sage's query tool returns
(series_summary, observations, numeric_claims, chart_spec), so the checks see
realistic input without needing the network.

Used by tools/characterise_answer_checks.py and tests/test_real_answers.py.
"""


def _years(first, last):
    return [str(year) for year in range(first, last + 1)]


_MONTHS = [
    f"{y} {m}"
    for y in (2025, 2026)
    for m in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
][:21]

# (table_id, title, unit, {series_no: (series_name, periods, values)})
TABLES = {
    "M015781": (
        "Share Of Nominal Gross Value Added, By Industry (SSIC 2020), Annual",
        "Per Cent",
        {
            "1": (
                "Goods Producing Industries",
                _years(1960, 2025),
                [
                    21.2,
                    21.5,
                    22.3,
                    23.1,
                    25.9,
                    26.7,
                    27.0,
                    27.4,
                    28.3,
                    29.0,
                    31.2,
                    32.6,
                    34.8,
                    34.4,
                    34.5,
                    34.6,
                    35.3,
                    35.0,
                    34.4,
                    36.0,
                    37.8,
                    37.2,
                    35.9,
                    37.0,
                    37.6,
                    34.4,
                    35.1,
                    34.5,
                    35.6,
                    34.3,
                    32.7,
                    34.4,
                    34.2,
                    34.3,
                    33.7,
                    33.9,
                    33.8,
                    33.3,
                    33.9,
                    32.9,
                    34.9,
                    32.4,
                    32.5,
                    32.0,
                    33.6,
                    32.9,
                    32.3,
                    30.0,
                    27.9,
                    28.5,
                    28.2,
                    26.8,
                    26.6,
                    24.9,
                    25.6,
                    25.8,
                    24.6,
                    25.0,
                    26.7,
                    25.3,
                    24.4,
                    25.9,
                    25.0,
                    23.4,
                    22.7,
                    24.1,
                ],
            ),
            "2": (
                "Services Producing Industries",
                _years(1960, 2025),
                [
                    75.4,
                    75.2,
                    74.5,
                    74.0,
                    71.0,
                    70.3,
                    70.1,
                    69.9,
                    69.1,
                    68.4,
                    66.0,
                    64.3,
                    62.1,
                    62.7,
                    62.9,
                    62.9,
                    62.2,
                    62.5,
                    63.2,
                    61.7,
                    60.3,
                    60.6,
                    62.2,
                    60.7,
                    59.5,
                    62.2,
                    61.3,
                    61.5,
                    60.6,
                    61.9,
                    63.7,
                    62.2,
                    62.2,
                    62.1,
                    62.3,
                    62.0,
                    62.0,
                    62.5,
                    61.5,
                    62.9,
                    61.4,
                    63.6,
                    63.9,
                    64.3,
                    63.3,
                    64.0,
                    64.6,
                    66.9,
                    68.2,
                    67.7,
                    68.1,
                    69.1,
                    68.9,
                    70.3,
                    69.7,
                    69.7,
                    71.2,
                    71.1,
                    69.6,
                    70.9,
                    71.6,
                    70.7,
                    72.0,
                    72.8,
                    73.4,
                    71.9,
                ],
            ),
            "3": (
                "Ownership Of Dwellings",
                _years(1960, 2025),
                [
                    3.4,
                    3.3,
                    3.2,
                    2.9,
                    3.1,
                    3.0,
                    2.9,
                    2.7,
                    2.6,
                    2.6,
                    2.8,
                    3.0,
                    3.2,
                    2.9,
                    2.6,
                    2.5,
                    2.5,
                    2.5,
                    2.4,
                    2.3,
                    1.9,
                    2.1,
                    1.8,
                    2.3,
                    2.9,
                    3.4,
                    3.6,
                    4.0,
                    3.8,
                    3.9,
                    3.6,
                    3.5,
                    3.6,
                    3.7,
                    4.0,
                    4.1,
                    4.2,
                    4.2,
                    4.6,
                    4.2,
                    3.7,
                    4.0,
                    3.6,
                    3.7,
                    3.1,
                    3.1,
                    3.1,
                    3.1,
                    3.9,
                    3.9,
                    3.7,
                    4.1,
                    4.5,
                    4.8,
                    4.7,
                    4.5,
                    4.2,
                    3.9,
                    3.7,
                    3.8,
                    4.1,
                    3.4,
                    3.0,
                    3.9,
                    3.9,
                    4.0,
                ],
            ),
        },
    ),
    "M451111": (
        "Merchandise Imports Of Machinery And Equipment, Monthly",
        "Thousand Dollars",
        {
            "1": (
                "Total Electronic Products",
                _MONTHS,
                [
                    21330228.1,
                    15766029.5,
                    18987883.2,
                    21508727.4,
                    18225053.5,
                    17330427.9,
                    22149459.1,
                    18244715.0,
                    21331152.5,
                    25428310.0,
                    21362478.1,
                    25610131.9,
                    25095253.6,
                    21553286.6,
                    30848393.1,
                    32977978.4,
                    34366439.9,
                    30617844.5,
                    31748785.9,
                    30093512.3,
                    40350081.0,
                ],
            ),
            "2": (
                "Non-Electronic Products",
                _MONTHS,
                [
                    9190707.2,
                    9153625.3,
                    11244669.7,
                    10563847.9,
                    10659637.0,
                    10696882.7,
                    11035187.9,
                    12021678.1,
                    10861374.0,
                    11733991.1,
                    11684338.9,
                    13540572.5,
                    12194041.4,
                    9990731.2,
                    11740786.3,
                    12170578.5,
                    11875286.7,
                    12466482.1,
                    14431367.5,
                    12753233.9,
                    12984217.5,
                ],
            ),
            "1.1": (
                "Integrated Circuits",
                _MONTHS,
                [
                    12296785.8,
                    10015145.4,
                    11130940.3,
                    11923180.0,
                    10888809.9,
                    10349441.0,
                    12329773.9,
                    10701078.6,
                    12739361.1,
                    15928950.6,
                    12123856.8,
                    14255401.8,
                    13905487.3,
                    11556132.7,
                    16675154.7,
                    17679486.3,
                    17889615.3,
                    17249428.1,
                    17401892.5,
                    18275923.3,
                    22186963.0,
                ],
            ),
        },
    ),
    "M081331": (
        "Research And Development Expenditure By Institutional Sector And Area Of "
        "Research, Annual",
        "Million Dollars",
        {
            "2": (
                "Private Sector",
                _years(1990, 2023),
                [
                    309.5,
                    442.0,
                    577.62,
                    618.58,
                    736.23,
                    881.38,
                    1133.42,
                    1314.52,
                    1536.1,
                    1670.86,
                    1866.05,
                    2045.02,
                    2055.01,
                    2053.62,
                    2569.56,
                    3018.54,
                    3281.74,
                    4222.1,
                    5105.45,
                    3689.06,
                    3761.11,
                    4446.29,
                    4238.01,
                    4315.42,
                    4989.14,
                    5469.39,
                    5295.59,
                    5325.15,
                    5484.22,
                    5916.67,
                    6602.9,
                    7277.41,
                    8130.09,
                    8955.99,
                ],
            ),
            "3": (
                "Institutes Of Higher Learning",
                _years(1990, 2023),
                [
                    119.7,
                    147.1,
                    156.03,
                    157.28,
                    179.53,
                    193.36,
                    238.71,
                    277.7,
                    305.84,
                    309.97,
                    338.27,
                    366.98,
                    430.01,
                    457.5,
                    424.71,
                    477.99,
                    575.67,
                    602.99,
                    709.77,
                    855.98,
                    973.64,
                    1091.55,
                    1199.42,
                    1319.37,
                    1356.58,
                    1583.42,
                    1656.06,
                    1663.5,
                    1654.83,
                    1598.54,
                    1461.61,
                    1486.08,
                    1716.71,
                    1872.64,
                ],
            ),
            "4": (
                "Government Sector (Including Public Research Institutes)",
                _years(1990, 2023),
                [
                    142.5,
                    167.6,
                    215.89,
                    222.08,
                    259.23,
                    291.83,
                    420.01,
                    512.33,
                    650.33,
                    675.46,
                    805.21,
                    820.68,
                    883.33,
                    885.77,
                    1047.19,
                    1072.88,
                    1141.04,
                    1501.11,
                    1298.32,
                    1464.04,
                    1573.29,
                    1733.89,
                    1636.82,
                    1758.72,
                    1961.49,
                    2154.76,
                    2184.81,
                    2013.48,
                    2059.71,
                    2175.24,
                    2340.65,
                    2601.62,
                    2829.4,
                    2918.28,
                ],
            ),
            "1.1": (
                "Agricultural & Food Sciences",
                _years(1994, 2023),
                [
                    12.51,
                    26.51,
                    14.89,
                    21.61,
                    23.32,
                    26.13,
                    29.67,
                    47.25,
                    29.21,
                    33.98,
                    45.13,
                    52.2,
                    58.28,
                    70.07,
                    95.06,
                    92.9,
                    108.49,
                    155.14,
                    199.95,
                    210.32,
                    262.93,
                    267.61,
                    280.78,
                    278.06,
                    364.53,
                    357.2,
                    349.44,
                    390.76,
                    400.45,
                    417.19,
                ],
            ),
            "1.3": (
                "Engineering & Technology",
                _years(1994, 2023),
                [
                    633.56,
                    807.29,
                    993.73,
                    1143.78,
                    1436.88,
                    1263.48,
                    1543.82,
                    1663.06,
                    1936.98,
                    1993.01,
                    2524.74,
                    2628.26,
                    2917.09,
                    4166.47,
                    4872.38,
                    3720.36,
                    3848.6,
                    4564.12,
                    4336.31,
                    4451.9,
                    4843.43,
                    5745.31,
                    5581.76,
                    5610.54,
                    5703.06,
                    6037.96,
                    6561.65,
                    7102.41,
                    7977.62,
                    8868.38,
                ],
            ),
            "1.4": (
                "Biomedical & Related Sciences",
                _years(1994, 2023),
                [
                    50.86,
                    37.63,
                    59.96,
                    84.3,
                    96.58,
                    113.68,
                    145.45,
                    297.74,
                    447.11,
                    497.28,
                    738.89,
                    853.41,
                    1068.04,
                    1057.61,
                    1062.53,
                    1259.13,
                    1361.65,
                    1357.75,
                    1403.59,
                    1367.58,
                    1577.95,
                    1762.31,
                    1779.36,
                    1710.97,
                    1685.72,
                    1872.19,
                    2055.39,
                    2351.98,
                    2377.84,
                    2306.11,
                ],
            ),
            "1.5": (
                "Natural Sciences (Excl Biological Sciences)",
                _years(1994, 2023),
                [
                    440.0,
                    389.13,
                    520.84,
                    614.98,
                    658.04,
                    807.9,
                    874.45,
                    688.24,
                    561.54,
                    477.84,
                    457.31,
                    471.33,
                    490.56,
                    587.01,
                    584.45,
                    649.11,
                    730.95,
                    853.83,
                    830.75,
                    918.37,
                    1093.85,
                    1027.45,
                    912.6,
                    822.24,
                    901.77,
                    1142.01,
                    1158.6,
                    1257.34,
                    1591.76,
                    1701.53,
                ],
            ),
            "1.10": (
                "Energy & Others",
                _years(1994, 2023),
                [
                    38.05,
                    106.0,
                    202.71,
                    239.89,
                    277.45,
                    445.11,
                    416.14,
                    536.39,
                    393.5,
                    394.8,
                    275.4,
                    564.21,
                    464.48,
                    445.04,
                    499.13,
                    287.59,
                    258.35,
                    340.88,
                    303.63,
                    445.34,
                    529.05,
                    404.9,
                    581.97,
                    580.32,
                    543.67,
                    281.09,
                    280.1,
                    262.61,
                    328.54,
                    453.69,
                ],
            ),
        },
    ),
}


def tool_result(table_id, series_nos, *, complete=True, url=None):
    """A successful query result for ``series_nos`` of ``table_id`` (real shape)."""
    title, unit, series = TABLES[table_id]
    summaries, observations, values = [], [], []
    for number in series_nos:
        name, periods, data = series[number]
        summaries.append(
            {
                "series_name": name,
                "series_no": number,
                "unit": unit,
                "latest_period": periods[-1],
                "latest_value": data[-1],
                "earliest_period": periods[0],
                "earliest_value": data[0],
                "change_latest_minus_earliest": round(data[-1] - data[0], 6),
                "observation_count": len(data),
            }
        )
        observations += [
            {"series_name": name, "series_no": number, "unit": unit, "period": p, "value": v}
            for p, v in zip(periods, data)
        ]
        values += data
    first_periods = series[series_nos[0]][1]
    return {
        "success": True,
        "table_id": table_id,
        "title": title,
        "table_complete": complete,
        "source_url": url or f"https://tablebuilder.singstat.gov.sg/table/TS/{table_id}",
        "series_search": "series id:" + ", series id:".join(series_nos),
        "selected_series": [
            {"series_no": n, "series_name": series[n][0], "unit": unit} for n in series_nos
        ],
        "series_summary": summaries,
        "observations": observations[:40],
        "numeric_claims": sorted(set(values)),
        "chart_kind": "time_series",
        "chart_spec": {
            "kind": "trend",
            "title": title,
            "unit": unit,
            "categories": first_periods,
            "series": [{"name": series[n][0], "values": series[n][2]} for n in series_nos],
        },
        "chart_source_verified": complete,
    }


def _money(value):
    return f"{value:,.2f}"


# The answer Sage displayed for question C (model prose that passed the checks).
ANSWER_C = """Singapore's R&D expenditure increased substantially over the historical period covered by SingStat Table M081331. The table reports annual expenditure in million Singapore dollars, with sector data from 1990-2023 and area-of-research data from 1994-2023.

### By institutional sector

| Institutional sector | Earliest value | 2023 value | Overall change |
|---|---:|---:|---:|
| Private sector | $309.50m in 1990 | $8,955.99m | +$8,646.49m |
| Government sector, including public research institutes | $142.50m in 1990 | $2,918.28m | +$2,775.78m |
| Institutes of higher learning | $119.70m in 1990 | $1,872.64m | +$1,752.94m |

The private sector dipped from $5,105.45 million in 2008 to $3,689.06 million in 2009, then reached a new high in 2023.

### By area of research

| Area of research | Earliest value | 2023 value | Overall change |
|---|---:|---:|---:|
| Engineering & Technology | $633.56m in 1994 | $8,868.38m | +$8,234.82m |
| Biomedical & Related Sciences | $50.86m in 1994 | $2,306.11m | +$2,255.25m |
| Natural Sciences, excluding biological sciences | $440.00m in 1994 | $1,701.53m | +$1,261.53m |
| Energy & Others | $38.05m in 1994 | $453.69m | +$415.64m |
| Agricultural & Food Sciences | $12.51m in 1994 | $417.19m | +$404.68m |

Source: SingStat Table Builder, M081331."""

# A latest-year category table, as the model writes for "latest breakdown" questions.
ANSWER_C_LATEST = """In 2023 (SingStat M081331), R&D expenditure by institutional sector was:

| Institutional sector | Million Dollars |
|---|---:|
| Private Sector | 8,955.99 |
| Institutes Of Higher Learning | 1,872.64 |
| Government Sector (Including Public Research Institutes) | 2,918.28 |
"""
ANSWER_C_RANKED = """Ranking of sectors in 2023 (SingStat M081331):

| Rank | Institutional sector | Million Dollars |
|---|---|---:|
| 1 | Private Sector | 8,955.99 |
| 2 | Government Sector (Including Public Research Institutes) | 2,918.28 |
| 3 | Institutes Of Higher Learning | 1,872.64 |
"""

_AREAS_2023 = [
    ("Engineering & Technology", 8868.38),
    ("Biomedical & Related Sciences", 2306.11),
    ("Natural Sciences (Excl Biological Sciences)", 1701.53),
    ("Energy & Others", 453.69),
    ("Agricultural & Food Sciences", 417.19),
]
_AREA_ROWS = "\n".join(f"| {name} | {value:,.2f} |" for name, value in _AREAS_2023)
# Latest-year table with a trailing total row: correct total, then a wrong one.
ANSWER_C_AREAS = (
    "R&D expenditure in 2023 (SingStat M081331) by research category:\n\n"
    "| Research category | Million Dollars |\n|---|---:|\n"
    + _AREA_ROWS
    + f"\n| Total | {sum(v for _, v in _AREAS_2023):,.2f} |\n"
)
ANSWER_C_AREAS_BAD_TOTAL = ANSWER_C_AREAS.replace(
    f"| Total | {sum(v for _, v in _AREAS_2023):,.2f} |", "| Total | 99,999.00 |"
)

QUESTIONS = {
    "M015781": [
        "How has the share of goods producing industries in gross value added changed over "
        "time?",
        "What is the make-up of nominal gross value added in Singapore, by industry, and "
        "how has it changed over the years?",
        "How has the share of goods producing industries changed since 1960?",
        "How have services producing industries changed over time?",
        "Compare goods producing and services producing industries over the years",
    ],
    "M451111": [
        "How have merchandise imports of electronic products changed since 2025?",
        "How have merchandise imports of machinery and equipment in Singapore changed "
        "month by month?",
        "How have imports of integrated circuits changed over time?",
        "Which category of machinery imports is largest in the latest month?",
    ],
    "M081331": [
        "How has private sector research and development expenditure changed over the " "years?",
        "How has research and development expenditure in Singapore changed over the years, "
        "by institutional sector and area of research?",
        "What is the latest breakdown of R&D expenditure by institutional sector?",
        "Which institutional sector spent the most on research and development in 2023?",
        "Compare R&D expenditure across institutional sectors in the latest year",
        "How has first-born research expenditure changed?",
        "What is the latest breakdown of R&D expenditure by research category?",
    ],
}

EVIDENCE_SETS = {
    "M015781": [["1"], ["2"], ["3"], ["1", "2"], ["1", "2", "3"]],
    "M451111": [["1"], ["2"], ["1.1"], ["1", "1.1"], ["1", "2"]],
    "M081331": [
        ["2"],
        ["3"],
        ["4"],
        ["2", "3", "4"],
        ["1.1", "1.3", "1.4", "1.5", "1.10"],
        ["1.3"],
    ],
}
