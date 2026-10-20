# Released Figure 8 evidence, collected 20 October 2026

639 journals × 2020–2025; primary journal source, OpenAlex core corpus,
all document types, any institution worldwide, no organisation restriction.

`journal_year_counts.csv` contains 3,834 journal/year rows: journal ID/title,
OpenAlex source ID, year, `total_works`, `unlinked_works`, `linked_works`, and
`percent_linked`. Unlinked means `institutions_distinct_count == 0`.
Calculate the missing-link percentage as `100 * unlinked_works / total_works`;
zero-work percentages are undefined (blank). Ten cells have no works.

- `openalex_pages.jsonl` retains dated query bodies, compact response groups,
  status and quota headers, including three initial transport failures.
  It contains no API credentials and is the explicit exception to the
  repository's cache ignore rule.
- `input_manifest.json` fixes the journal hash, corpus, filters and request plan.
- `collection_summary.json` retains the original collection audit and script
  hashes: 54 successful requests and 72 independent checked values.
- `figure_papers_with_institutions_data.csv` contains annual counts, journal
  mean/median/percentiles and a pooled work-weighted linked percentage.
- `figure_papers_with_institutions.*` is the annual figure (PNG, PDF, SVG).
- `figure_journal_percent_linked_histograms_data.csv` contains all 60 bins;
  the corresponding figures show six annual journal distributions. Bins include
  the lower bound and exclude the upper, except the last bin includes 100%.
- `CAPTION_AND_RESULTS.txt`, `HISTOGRAM_CAPTION.txt` describe the figures.
- `verification_summary.json` records independent checks and artifact hashes.
  Reproduction refreshes this audit and may change image hashes with fonts or
  rendering-library versions; the count CSV's original hash remains unchanged.

The overall unweighted mean linked percentage across 3,824 nonempty cells is
91.7706%; the pooled work-weighted linked percentage is 94.9945%. These are
different statistics. A linked work may still have missing or wrong affiliations.

See [the parent README](../README.md) for exact offline commands, fresh collection
in a separate output directory, dependencies and method. The adapted public
scripts' provenance is in `../release_provenance.json`; original collector hashes
in this folder describe the original local collection, not the adapted scripts.
