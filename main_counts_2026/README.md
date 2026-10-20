# October 2026 recollection and institution-link analysis

This folder releases the existing October collector, its study inputs,
compact count matrices and Figure 8 replication materials. Run all commands from
the repository root. Install dependencies as described in the [root README](../README.md).

## Study inputs and released results

- `orgs.csv`: 220 organisations with OpenAlex IDs and Scopus lineage queries.
- `journals.csv`: 639 journals selected by `should_include_20_25 == TRUE`, one
  source ID per database per journal. Input order determines Scopus batches.
- `pilot_plan.json`: the actual three October pilot selections (organisation
  index and source batch); historical volumes are selection context only.
- `openalex_counts.csv`, `scopus_counts.csv`: each has 1,320 organisation/year
  rows, 639 source columns, and 843,480 count cells. Columns are `year`,
  `openalex_id`, then source IDs for that database. Use `journals.csv` to map IDs.
- `validation_summary.json`: original verification, input/output hashes and
  dated request audit from 2 October 2026. Hashes for the omitted large combined
  export and pilot validation refer to the original local artifacts.
- `release_provenance.json`: source-script hashes, public-script changes and
  hashes of released inputs/data. Original run summaries retain their original
  collector hashes; those are not hashes of the adapted public scripts.

| Year | Scopus institutional counts | OpenAlex institutional counts |
|---|---:|---:|
| 2020 | 37,867 | 36,028 |
| 2021 | 39,408 | 37,261 |
| 2022 | 37,454 | 34,621 |
| 2023 | 38,364 | 35,409 |
| 2024 | 40,752 | 37,443 |
| 2025 | 42,274 | 39,515 |
| Total | 236,119 | 220,277 |

These are full institutional counts: collaborative papers may count for several
organisations. They are not unique-paper totals across the study.

## Fresh collection and resume

Credentials are `OPENALEX_API_KEY` and/or comma-separated `SCOPUS_API_KEYS`
(`SCOPUS_API_KEY` is also accepted). The optional ignored root `secrets.txt`
supports `openalex_api_key` and `scopus_api_key1`, `scopus_api_key2`, etc., with
`name=value` or `name: value` lines. Environment variables take precedence.
Do not place actual key values in tracked files or command arguments.

Use a new run directory for a new collection. These commands execute real API
requests at `pilot`, `openalex`, and `scopus`; all other stages are offline:

```powershell
python main_counts_2026/collect.py prepare --run-dir runs/october
python -u main_counts_2026/collect.py pilot --run-dir runs/october
python -u main_counts_2026/collect.py openalex --run-dir runs/october
python -u main_counts_2026/collect.py scopus --workers 16 --run-dir runs/october
python main_counts_2026/collect.py export_openalex --run-dir runs/october
python main_counts_2026/collect.py export_scopus --run-dir runs/october
python main_counts_2026/collect.py export --run-dir runs/october
python main_counts_2026/collect.py status --run-dir runs/october
python main_counts_2026/plot_linkage.py --run-dir runs/october
```

The Scopus pilot is required before full Scopus collection. OpenAlex can run
independently without it. The pilot checks identical Scopus query totals across
supplied credentials, then compares six annual batch counts and six annual
single-source counts for each of three partitions against direct queries.
Its cumulative log ceiling is 100 attempts; the original revised pilot used
67 requests including diagnostics.

Repeat the same collection command to resume completed partitions and cached
successful responses. Keep inputs, settings and the Scopus credential order
unchanged. Do not copy an old successful pilot validation into a fresh run.
The input manifest refuses changed input hashes. A `*_stop.json` blocks
dispatch until the cause is reviewed; do not remove it without addressing the
recorded condition. `status` makes no API calls. Running `prepare` alone makes
no requests and does not require credentials.

Each database has a shared four-requests/second limiter. OpenAlex defaults to
four workers; the original Scopus run used 16. Avoid overlapping collectors
using the same allowance. The first 429 stops further dispatch without a retry;
in-flight requests can finish. The client reserves at least 100 OpenAlex
credits and 250 Scopus requests per credential, plus a scheduling margin.
Transport/5xx retries are bounded at three attempts; access and invalid-query
errors halt. The original run used 1,377 successful OpenAlex requests and logged
10,373 Scopus requests including pilot/diagnostics, with no 429s. These are
observed October values, not guarantees for another run.

## Counting method and validation

OpenAlex POSTs grouped queries to `https://api.openalex.org/` using the OQO query
body: one organisation/year and all 639 primary source IDs, grouped by
`primary_location.source.id`. Institution membership uses
`authorships.institutions.lineage`. The original collector uses the API's default
works corpus with no publication-type filter. At most four pages of 200 groups
are accepted; source buckets must be unique and their sum must equal `meta.count`
before missing buckets become zero.

Scopus uses the supplied lineage expression, a batch of at most 100 source IDs,
and `PUBYEAR > 2019 AND PUBYEAR < 2026`. STANDARD results use offset pages of 25
and the sort `+coverDate,+publicationName,+creator`. A credential stays fixed
within each partition. The collector requires unique EIDs, expected sources,
valid cover-date years, constant query totals and a complete record tally.
Annual counts use the year of `prism:coverDate`, checked against PUBYEAR queries
in the pilot. Partitions beyond 5,000 records split by year, then source IDs.
Duplicate EIDs, invalid cover dates, or an unsplittable oversized source/year
trigger an explicitly labelled direct-count fallback with source/batch/annual
reconciliation. The original completed run needed no fallback.

`export_openalex` and `export_scopus` require all partitions for their database.
`export` requires both complete databases and writes `annual_counts.csv` with
843,480 rows. Columns identify organisation, journal, year, both source IDs and
both counts. This 115 MB combined table is generated locally rather than stored
in Git. Export the matrices before `export` so the generated validation summary
also contains the matrix hashes used by `plot_linkage.py`.

The full run's raw page caches and partition files are not released here. The
released matrices permit offline reproduction of the institutional-count figure;
a fresh collection regenerates the full combined table and request evidence.
Run-local files are excluded under `runs/`.

## Papers without any institution links (Figure 8)

`journal_institution_coverage.py` counts papers with **zero institution links
anywhere in OpenAlex**, using `institutions_distinct_count == 0`. It uses the
same 639 journals and 2020–2025, **without an organisation filter**, the explicit
`core` corpus, all document types, and `primary_location.source.id`.
This is not a count of papers missing links to the 220 study organisations.

For each journal/year it collects `total_works` and `unlinked_works`, then derives
`linked_works = total_works - unlinked_works`. Fresh collections write both
`percent_unlinked = 100 * unlinked_works / total_works` and `percent_linked`.
The original released count table keeps its original schema and hash; its
unlinked percentage is `100 - percent_linked`. Both percentages are undefined
for zero-work cells (blank, not zero).

The annual figure shows the unweighted journal mean, median and empirical
25–75, 10–90 and 5–95 percentile bands of **linked** percentages (NumPy linear
interpolation). Bands are not confidence intervals. Annual CSVs also supply
total/unlinked/linked work counts and `pooled_percent_linked`; the pooled
unlinked percentage is `100 * unlinked_works / total_works`, distinct from
the unweighted mean across journals.

| Year | Total works | Unlinked works | Pooled unlinked (%) |
|---|---:|---:|---:|
| 2020 | 266,788 | 15,233 | 5.7098 |
| 2021 | 271,469 | 12,526 | 4.6142 |
| 2022 | 267,846 | 11,823 | 4.4141 |
| 2023 | 267,351 | 11,977 | 4.4799 |
| 2024 | 296,607 | 14,896 | 5.0221 |
| 2025 | 317,934 | 18,038 | 5.6735 |

The released evidence is in [`journal_institution_coverage/`](journal_institution_coverage/README.md).
Offline reproduction needs no credentials:

```powershell
python main_counts_2026/journal_institution_coverage.py plot
python main_counts_2026/journal_institution_coverage.py histograms
python main_counts_2026/journal_institution_coverage.py verify
```

For a fresh OpenAlex collection, use a separate output directory:

```powershell
python main_counts_2026/journal_institution_coverage.py collect --output-dir runs/institution_coverage
python main_counts_2026/journal_institution_coverage.py plot --output-dir runs/institution_coverage
python main_counts_2026/journal_institution_coverage.py histograms --output-dir runs/institution_coverage
python main_counts_2026/journal_institution_coverage.py verify --output-dir runs/institution_coverage
```

The request plan allows 48 main grouped requests plus 12 independent grouped-year
validation queries, capped at 60 logged attempts including retries/resume. The
original run used 54 successful requests and three initial transport failures.
Six journals validate 72 values. Absent buckets become zero only after totals
reconcile; every cell must satisfy `0 <= unlinked <= total`.
`verify` reconciles all 3,834 rows with saved response buckets and independently
checks annual counts, means, medians, percentiles and histogram bins. Ten empty
journal-years are excluded from percentage distributions.

Presence of any link establishes neither correctness nor completeness of
affiliation assignments. Earlier figures using `locations.source.id` measure a
different journal definition and cannot be treated as a direct database-change
comparison with this primary-source series.

## Verification and API references

```powershell
python -m unittest discover -s tests -v
python -m unittest discover -s main_counts_2026 -p "test_*.py" -v
```

Tests use mock responses and no API allowance. The October tests cover paging,
duplicate/invalid records, changed totals, incomplete responses, direct fallback,
first-429 stopping, reserve switching/cache resume, public input preparation,
independent credentials and credential redaction.

- [OpenAlex authentication](https://help.openalex.org/api/authentication/)
- [OpenAlex grouping](https://help.openalex.org/api/grouping/)
- [Scopus Search API](https://dev.elsevier.com/documentation/SCOPUSSearchAPI.wadl)
- [Elsevier API quota settings](https://dev.elsevier.com/api_key_settings.html)
