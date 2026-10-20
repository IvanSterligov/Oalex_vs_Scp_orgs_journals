# OpenAlex and Scopus data collection

This repository is supplementary material for the paper *Comparison of OpenAlex
and Scopus coverage of German institutions’ publications in top-tier journals*
by Ivan Sterligov and Andrey Lovakov, as presented at the STI 2026 conference
in Antwerp.

This folder contains the inputs, code, and reference data used to collect yearly
publication counts for 220 German research organisations and 712 journals. The
collection years are 2020–2024. The scripts reproduce data collection, not the
paper's statistical analysis.

## Files

- `openalex_counts.py` collects OpenAlex counts.
- `scopus_counts.py` collects Scopus counts.
- `data/orgs.csv` contains the 220 organisations, OpenAlex IDs, and Scopus
  lineage queries.
- `data/journals.csv` maps the 712 OpenAlex source IDs to Scopus source IDs.
- `data/OpenData.xlsx` is the released historical count dataset.
- `tests/test_collectors.py` tests query parsing, zero handling, key rotation,
  and checkpoint/resume behavior without calling either API.

## Requirements

- Python 3.10 or newer
- packages listed in `requirements.txt`
- an OpenAlex API key
- one or more Scopus API keys with access to the Scopus Search API

Create an environment and install the dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Provide credentials through environment variables. Values are read at runtime
and are never written to outputs or logs:

```powershell
$env:OPENALEX_API_KEY = "your-key"
$env:SCOPUS_API_KEYS = "first-key,second-key"
```

`SCOPUS_API_KEYS` may contain one key or a comma-separated list. The Scopus
collector rotates to the next key only when the API reports quota exhaustion.

## Small test collection

Run the unit tests first:

```powershell
python -m unittest discover -s tests -v
```

Then collect a small 3 × 3 subset:

```powershell
python openalex_counts.py --max-orgs 3 --max-journals 3 --output openalex_smoke.tsv
python scopus_counts.py --max-orgs 3 --max-journals 3 --output scopus_smoke.tsv
```

Both output files contain one row per organisation and year. Journal columns
use OpenAlex source IDs in both files so that the matrices can be compared
directly. `data/journals.csv` supplies the corresponding Scopus source IDs.

## Full collection

From this folder, run:

```powershell
python openalex_counts.py --output openalex_counts.tsv
python scopus_counts.py --output scopus_counts.tsv
```

OpenAlex supports grouping by publication year. The OpenAlex script therefore
uses one request for each organisation–journal pair and fills absent year
groups with zero. For 220 organisations and 712 journals this is 156,640
requests instead of 783,200 separate yearly requests.

The free Scopus Search API does not provide access to publication-year facets.
The Scopus script first requests the total for 2020–2024. If that total is zero,
all five years are set to zero. If it is positive, the script requests the five
individual years. A failed request is left missing; it is never converted to a
zero.

The scripts write an atomic checkpoint after each batch. If a daily OpenAlex
budget or all weekly Scopus quotas are exhausted, rerun the same command with
`--resume` after the quota resets:

```powershell
python openalex_counts.py --output openalex_counts.tsv --resume
python scopus_counts.py --output scopus_counts.tsv --resume
```

Do not change the input files between an interrupted run and its resume.

## Method notes

- OpenAlex institution counts use `authorships.institutions.lineage`.
- Journal membership uses `primary_location.source.id`, which represents the
  source that published the work.
- Scopus organisation searches use the supplied lineage expressions and
  `SOURCE-ID` journal filters.
- No publication-type filter is applied, matching the paper's method.
- Live databases are updated continuously, so new collections may not exactly
  equal the historical values in `OpenData.xlsx`.

Current API documentation:

- OpenAlex authentication: https://help.openalex.org/api/authentication/
- OpenAlex grouping: https://help.openalex.org/api/grouping/
- Scopus Search API: https://dev.elsevier.com/documentation/SCOPUSSearchAPI.wadl
- Elsevier quotas: https://dev.elsevier.com/api_key_settings.html
