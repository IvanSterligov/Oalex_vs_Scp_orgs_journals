# OpenAlex and Scopus: institutional and journal coverage

Supplementary code and data for *Comparison of OpenAlex and Scopus coverage of
German institutions’ publications in top-tier journals* by Ivan Sterligov and
Andrey Lovakov, presented at STI 2026 in Antwerp.

The **October 2026 recollection** is in [`main_counts_2026/`](main_counts_2026/README.md):
220 German research organisations, 639 selected journals, publication years
2020–2025, all document types. Both databases were recollected on **2 October
2026**. The OpenAlex analysis of papers **without any institution links** was
collected separately on **20 October 2026**.

The earlier 712-journal, 2020–2024 collectors and historical `data/OpenData.xlsx`
remain available; see [the earlier workflow](LEGACY_COLLECTION.md). Its input
files in `data/` describe that earlier cohort, not the 639-journal October cohort.
The released scripts collect counts and reproduce the documented figures; they
do not implement every analysis in the paper.

## Setup

Use Python 3.13 for the tested October reproduction environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -r main_counts_2026/requirements-reproduction.txt
python -m unittest discover -s tests -v
python -m unittest discover -s main_counts_2026 -p "test_*.py" -v
```

`requirements.txt` contains compatible dependency ranges; the second file pins
the three dependencies used for the October collectors and figures. No new
service or framework is required.

## Reproduce released figures without API access

```powershell
python main_counts_2026/plot_linkage.py
python main_counts_2026/journal_institution_coverage.py plot
python main_counts_2026/journal_institution_coverage.py histograms
python main_counts_2026/journal_institution_coverage.py verify
```

The repository includes both compact institutional count matrices and the
journal-wide count table with its compact OpenAlex response evidence. The first
figure is the ratio of summed OpenAlex to Scopus **institutional counts**. Figure
8 measures the presence of **any institutional link worldwide**, with no filter
for the 220 organisations. These are different denominators.

## Collect fresh counts

Set credentials locally, then follow the [October collection instructions](main_counts_2026/README.md):

```powershell
$env:OPENALEX_API_KEY = "your-openalex-key"
$env:SCOPUS_API_KEYS = "first-scopus-key,second-scopus-key"
```

Only the credential for the selected database is required. Figure 8 needs only
OpenAlex. Use the documented `runs/` directories for fresh collection so the
released data stay available. Live databases change, so fresh counts can differ.

`.gitignore` excludes `.env` files, `secrets.txt`, `keys.txt`, other common
credential files, environments, and new collection caches. Authentication is
sent in request headers; the October client redacts credential values from
response logs. The included historical response evidence was scanned before
release. Never paste actual credentials into scripts, documentation, command
arguments, or tracked files; ignoring a file does not remove it from existing
Git history.

See [the October README](main_counts_2026/README.md) for query definitions,
request limits, validation, output schemas, provenance, and exact commands.
