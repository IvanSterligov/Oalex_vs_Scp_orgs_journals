"""Collect and plot journal-wide institutional coverage using the existing client."""

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

import collect as collection


HERE = Path(__file__).resolve().parent
OUT = HERE / "journal_institution_coverage"
YEARS = list(range(2020, 2026))
SOURCE_FIELD = "primary_location.source.id"
INPUT_HASH = "7085e2ee754bcacfdc8cf7ea9fec557c08706f489dc866bde145fd1505a1be8e"
STEM = "figure_papers_with_institutions"
HIST_STEM = "figure_journal_percent_linked_histograms"
CAP = 60


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def journals():
    path = HERE / "journals.csv"
    if digest(path) != INPUT_HASH:
        raise ValueError("Journal snapshot differs from the specified 639-journal input")
    rows = collection.read_csv(path)
    sources = [r["openalex_source_ids"] for r in rows]
    if len(rows) != 639 or len(set(sources)) != 639:
        raise ValueError("Expected 639 unique journal sources")
    if not all(r["should_include_20_25"] == "TRUE" for r in rows):
        raise ValueError("Unexpected journal inclusion flag")
    return rows, sources


def query(filters, group):
    return {"get_rows": "works", "corpus": "core", "filter_rows": filters,
            "group_by": [{"column_id": group}]}


def grouped(client, filters, group, allowed):
    """Only fill absent buckets with zero after all work counts reconcile."""
    counts, cursors = {}, set()
    cursor, total = "*", None
    for _ in range(math.ceil(len(allowed) / 200)):
        payload = client.request({"oqo": query(filters, group), "per_page": 200, "cursor": cursor})
        new_total = int(payload["meta"]["count"])
        if new_total < 0 or (total is not None and new_total != total):
            raise ValueError("Invalid or changing grouped query total")
        total = new_total
        for bucket in payload["group_by"]:
            key = str(bucket["key"]).rsplit("/", 1)[-1]
            count = int(bucket["count"])
            if key not in allowed or key in counts or count < 0:
                raise ValueError("Unexpected, duplicate or negative group bucket")
            counts[key] = count
        if sum(counts.values()) == total:
            return {key: counts.get(key, 0) for key in allowed}
        cursor = payload["meta"].get("next_cursor")
        if not cursor or cursor in cursors or not payload["group_by"]:
            raise ValueError("Incomplete grouped response")
        cursors.add(cursor)
    raise ValueError("Group pages exceeded the planned maximum")


def year_filters(year, sources):
    return [{"column_id": "publication_year", "value": year},
            {"join": "or", "filters": [{"column_id": SOURCE_FIELD, "value": s} for s in sources]}]


def unlinked_filter():
    return {"column_id": "institutions_distinct_count", "value": 0}


def collect():
    rows, sources = journals()
    OUT.mkdir(exist_ok=True)
    manifest_path = OUT / "input_manifest.json"
    manifest = {"journal_sha256": INPUT_HASH, "journals": 639, "years": YEARS,
                "source_field": SOURCE_FIELD, "corpus": "core", "document_types": "all",
                "institution_scope": "any institution worldwide; no organisation filter",
                "unlinked_filter": unlinked_filter(), "max_attempts": CAP,
                "main_request_ceiling": 48, "validation_request_ceiling": 12}
    if manifest_path.exists():
        if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
            raise ValueError("Collection specification changed")
    else:
        collection.save(manifest_path, manifest)

    # Redirect only this process's existing resumable client to the new run folder.
    collection.HERE = OUT
    key, _ = collection.credentials("openalex")
    log = OUT / "openalex_pages.jsonl"
    existing_attempts = len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0
    client = collection.Client("openalex", [key], rps=2, cap=max(0, CAP - existing_attempts))
    values = {}
    for year in YEARS:
        base = year_filters(year, sources)
        totals = grouped(client, base, SOURCE_FIELD, set(sources))
        missing = grouped(client, base + [unlinked_filter()], SOURCE_FIELD, set(sources))
        for source in sources:
            if missing[source] > totals[source]:
                raise ValueError("Unlinked count exceeds total")
            values[year, source] = totals[source], missing[source]
        print(f"{year}: {sum(totals.values()):,} works; {sum(missing.values()):,} unlinked", flush=True)

    # Six journals spanning the earlier period-volume distribution, queried in
    # the other direction (one source, group by year) to check the main results.
    ordered = sorted(rows, key=lambda r: (int(r["openalex_2020_2025"]), r["openalex_source_ids"]))
    checked = []
    for index in (0, 127, 255, 383, 511, 638):
        row = ordered[index]
        source = row["openalex_source_ids"]
        base = [{"column_id": SOURCE_FIELD, "value": source},
                {"column_id": "publication_year", "operator": ">=", "value": 2020},
                {"column_id": "publication_year", "operator": "<=", "value": 2025}]
        totals = grouped(client, base, "publication_year", {str(y) for y in YEARS})
        missing = grouped(client, base + [unlinked_filter()], "publication_year", {str(y) for y in YEARS})
        for year in YEARS:
            if values[year, source] != (totals[str(year)], missing[str(year)]):
                raise ValueError(f"Independent grouped-year check failed for {source}/{year}")
        checked.append({"source": source, "title": row["journal_title"], "all_six_years_match": True})
    data = []
    for row in rows:
        source = row["openalex_source_ids"]
        for year in YEARS:
            total, missing = values[year, source]
            data.append({"npi_journal_id": row["npi_journal_id"], "journal_title": row["journal_title"],
                         "openalex_source_id": source, "year": year, "total_works": total,
                         "unlinked_works": missing, "linked_works": total - missing,
                         "percent_unlinked": 100 * missing / total if total else "",
                         "percent_linked": 100 * (total - missing) / total if total else ""})
    path = OUT / "journal_year_counts.csv"
    collection.write_csv(path, data, list(data[0]))
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    collection.save(OUT / "collection_summary.json", {
        "completed_at_utc": collection.now(), "complete": True, "rows": len(data),
        "journal_year_counts_sha256": digest(path), "journal_sha256": INPUT_HASH,
        "request_attempts": len(records), "http_statuses": dict(Counter(str(r["status"]) for r in records)),
        "reported_credits_used": sum(float(r.get("headers", {}).get("x-ratelimit-credits-used", 0)) for r in records),
        "first_request_utc": records[0]["time"], "last_request_utc": records[-1]["time"],
        "last_quota_headers": records[-1]["headers"], "validation_journals": checked,
        "collector_sha256": digest(Path(__file__)), "shared_client_sha256": digest(HERE / "collect.py")})
    print(f"Validated 72 counts across six journals; {len(records)} total request attempts.", flush=True)


def plot():
    _, sources = journals()
    summary = json.loads((OUT / "collection_summary.json").read_text(encoding="utf-8"))
    path = OUT / "journal_year_counts.csv"
    if not summary["complete"] or digest(path) != summary["journal_year_counts_sha256"]:
        raise ValueError("Unverified journal-year counts")
    data = collection.read_csv(path)
    if len(data) != 639 * 6 or {(int(r["year"]), r["openalex_source_id"]) for r in data} != {(y, s) for y in YEARS for s in sources}:
        raise ValueError("Incomplete journal-year coverage")
    annual = []
    all_percentages = []
    for year in YEARS:
        subset = [r for r in data if int(r["year"]) == year]
        for r in subset:
            total, missing, linked = (int(r[k]) for k in ("total_works", "unlinked_works", "linked_works"))
            if not (0 <= missing <= total and linked == total - missing):
                raise ValueError("Invalid linked/unlinked counts")
        percentages = np.array([100 * int(r["linked_works"]) / int(r["total_works"]) for r in subset if int(r["total_works"]) > 0])
        all_percentages.extend(percentages)
        quantiles = np.percentile(percentages, [5, 10, 25, 75, 90, 95], method="linear")
        total = sum(int(r["total_works"]) for r in subset)
        missing = sum(int(r["unlinked_works"]) for r in subset)
        annual.append({"year": year, "journals_with_works": len(percentages), "zero_work_journals": 639 - len(percentages),
                       "mean_percent_linked": float(np.mean(percentages)),
                       "median_percent_linked": float(np.median(percentages)),
                       **{f"p{p}_percent_linked": float(v) for p, v in zip([5, 10, 25, 75, 90, 95], quantiles)},
                       "total_works": total, "unlinked_works": missing, "linked_works": total - missing,
                       "pooled_percent_linked": 100 * (total - missing) / total})
    collection.write_csv(OUT / f"{STEM}_data.csv", annual, list(annual[0]))
    plt.rcParams.update({"font.family": "Arial", "font.size": 11})
    fig, ax = plt.subplots(figsize=(10, 4.8))
    handles = {}
    for low, high, opacity in [(5, 95, .15), (10, 90, .25), (25, 75, .40)]:
        label = f"{low}–{high} percentile"
        handles[label] = ax.fill_between(YEARS, [r[f"p{low}_percent_linked"] for r in annual],
                                        [r[f"p{high}_percent_linked"] for r in annual],
                                        color="#1f77b4", alpha=opacity, linewidth=0, label=label)
    handles["Mean"], = ax.plot(YEARS, [r["mean_percent_linked"] for r in annual], color="#0072BD", linewidth=1.8, label="Mean")
    handles["Median"], = ax.plot(YEARS, [r["median_percent_linked"] for r in annual], color="#8C3B00",
                               linewidth=1.8, linestyle="--", label="Median")
    ax.set(title="% papers with institutions (mean, median + percentile bands)", xlabel="Year", ylabel="Percent")
    ax.set_xticks(YEARS)
    lowest = min(r["p5_percent_linked"] for r in annual)
    lower_limit = min(80, 5 * math.floor(lowest / 5))
    ax.set_ylim(lower_limit, 100)
    ax.set_yticks(np.arange(lower_limit, 101, 2 if lower_limit >= 80 else 5))
    ax.grid(axis="y", color="#D0D0D0", alpha=.6, linewidth=.8)
    ax.set_axisbelow(True)
    order = ["25–75 percentile", "10–90 percentile", "5–95 percentile", "Mean", "Median"]
    ax.legend([handles[label] for label in order], order, frameon=False, loc="center left", bbox_to_anchor=(1.02, .5))
    fig.subplots_adjust(left=.085, right=.76, bottom=.14, top=.9)
    for extension in ("png", "pdf", "svg"):
        fig.savefig(OUT / f"{STEM}.{extension}", dpi=300, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    text = (
        "Figure 8. Yearly distribution of the 639 surveyed journals by the percentage of OpenAlex works "
        "linked to at least one institution ID, 2020–2025. The solid blue line shows the unweighted mean across "
        "journals and the dashed brown line the median; "
        "shading shows the 25–75, 10–90 and 5–95 percentile ranges. All document types in the default core corpus "
        "are included; journal membership uses primary_location.source.id. Journal-years with no works are "
        "excluded from the distributions. Counts were collected on 20 October 2026.\n\n"
        f"Across the {len(all_percentages):,} journal-year observations with at least one work, the mean share "
        f"linked to an institution was {np.mean(all_percentages):.2f}%. The annual mean was "
        f"{annual[0]['mean_percent_linked']:.2f}% in 2020 and {annual[-1]['mean_percent_linked']:.2f}% in 2025 "
        f"(change: {annual[-1]['mean_percent_linked'] - annual[0]['mean_percent_linked']:+.2f} percentage points). "
        f"The median was {annual[0]['median_percent_linked']:.2f}% in 2020 and "
        f"{annual[-1]['median_percent_linked']:.2f}% in 2025. "
        "This measures the presence of any institutional link, rather than the correctness or completeness "
        "of author affiliations.\n"
    )
    (OUT / "CAPTION_AND_RESULTS.txt").write_text(text, encoding="utf-8")
    for row in annual:
        print(f"{row['year']}: mean {row['mean_percent_linked']:.4f}%; median {row['median_percent_linked']:.4f}%; n={row['journals_with_works']}")


def histograms():
    """Plot six journal distributions with common 10-percentage-point bins."""
    verify()
    data = collection.read_csv(OUT / "journal_year_counts.csv")
    edges = np.arange(0, 101, 10)
    annual, exported = [], []
    for year in YEARS:
        rows = [r for r in data if int(r["year"]) == year]
        values = [float(r["percent_linked"]) for r in rows if r["percent_linked"] != ""]
        counts, _ = np.histogram(values, bins=edges)
        if int(sum(counts)) != len(values):
            raise ValueError("Histogram lost values outside 0–100%")
        annual.append((year, values, counts, len(rows) - len(values)))
        for index, count in enumerate(counts):
            low, high = int(edges[index]), int(edges[index + 1])
            exported.append({"year": year, "bin_lower_percent": low, "bin_upper_percent": high,
                             "bin_upper_inclusive": index == len(counts) - 1,
                             "journal_count": int(count), "percent_of_nonempty_journals": 100 * int(count) / len(values),
                             "journals_with_works": len(values), "zero_work_journals_excluded": len(rows) - len(values)})
    collection.write_csv(OUT / f"{HIST_STEM}_data.csv", exported, list(exported[0]))
    plt.rcParams.update({"font.family": "Arial", "font.size": 10})
    fig, axes = plt.subplots(2, 3, figsize=(12, 7.2), sharex=True, sharey=True)
    highest = max(int(max(counts)) for _, _, counts, _ in annual)
    ymax = 50 * math.ceil((highest + 35) / 50)
    for ax, (year, values, counts, excluded) in zip(axes.flat, annual):
        bars = ax.bar(edges[:-1], counts, width=10, align="edge", color="#4B8BBE", edgecolor="white", linewidth=.9)
        ax.bar_label(bars, labels=[str(int(c)) if c else "" for c in counts], padding=3, fontsize=9)
        ax.set_title(str(year), pad=10)
        ax.set_xlim(0, 100)
        ax.set_ylim(0, ymax)
        ax.set_xticks(range(0, 101, 20))
        ax.set_xticks(range(0, 101, 10), minor=True)
        ax.tick_params(axis="x", labelbottom=True)
        ax.set_yticks(range(0, ymax + 1, 100))
        ax.grid(axis="y", color="#D0D0D0", linewidth=.7, alpha=.65)
        ax.set_axisbelow(True)
        ax.spines[["top", "right"]].set_visible(False)
    fig.suptitle("Distribution of journals by institutional-link coverage", fontsize=16, y=.98)
    fig.supxlabel("Works linked to at least one institution (%)", y=.07)
    fig.supylabel("Number of journals", x=.02)
    fig.text(.5, .018, "10-percentage-point bins; the 90–100% bin includes 100%.",
             ha="center", fontsize=9, color="#444444")
    fig.subplots_adjust(left=.08, right=.98, top=.88, bottom=.17, hspace=.34, wspace=.13)
    for extension in ["png", "pdf", "svg"]:
        fig.savefig(OUT / f"{HIST_STEM}.{extension}", dpi=300, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    (OUT / "HISTOGRAM_CAPTION.txt").write_text(
        "Distribution of the surveyed journals by the percentage of OpenAlex works linked to at least one institution ID, "
        "separately for 2020–2025. Each panel is a histogram with 10-percentage-point bins and common axes. "
        "Bins include their lower boundary and exclude their upper boundary, except the final 90–100% bin, which "
        "includes 100%. Each nonempty journal-year contributes one observation. Journal-years with zero works are "
        "excluded (0, 1, 1, 2, 3 and 3 respectively). Primary journal source; core corpus; all document types; "
        "no institution restriction. Data collected 20 October 2026.\n", encoding="utf-8")
    print("Saved six histograms and 60 bin-count rows; no new API requests.")


def verify():
    """Independently check saved responses and statistics without API credentials."""
    _, sources = journals()
    data = collection.read_csv(OUT / "journal_year_counts.csv")
    annual = collection.read_csv(OUT / f"{STEM}_data.csv")
    records = [json.loads(line) for line in (OUT / "openalex_pages.jsonl").read_text(encoding="utf-8").splitlines()]
    raw = {}
    for record in records:
        if record["status"] != 200:
            continue
        q = record["request"]["oqo"]
        if q["group_by"][0]["column_id"] != SOURCE_FIELD:
            continue
        filters = q["filter_rows"]
        year = next(f["value"] for f in filters if f.get("column_id") == "publication_year")
        unlinked = any(f.get("column_id") == "institutions_distinct_count" for f in filters)
        for bucket in record["payload"]["group_by"]:
            key = year, bucket["key"].rsplit("/", 1)[-1], unlinked
            if key in raw:
                raise ValueError("Repeated main-query source bucket")
            raw[key] = int(bucket["count"])
    expected = {(year, source) for year in YEARS for source in sources}
    if len(data) != len(expected) or {(int(r["year"]), r["openalex_source_id"]) for r in data} != expected:
        raise ValueError("Incomplete journal-year grid")
    for row in data:
        year, source = int(row["year"]), row["openalex_source_id"]
        total, missing, linked = (int(row[k]) for k in ["total_works", "unlinked_works", "linked_works"])
        if (total, missing) != (raw.get((year, source, False), 0), raw.get((year, source, True), 0)):
            raise ValueError("Counts do not match saved API buckets")
        if not 0 <= missing <= total or linked != total - missing:
            raise ValueError("Invalid linked/unlinked counts")
        if total and not math.isclose(float(row["percent_linked"]), 100 * linked / total, abs_tol=1e-12):
            raise ValueError("Invalid journal percentage")
        if total == 0 and row["percent_linked"] != "":
            raise ValueError("Zero-work percentage must be blank")
        if "percent_unlinked" in row:
            expected_missing = 100 * missing / total if total else ""
            if (not total and row["percent_unlinked"] != "") or (total and not math.isclose(float(row["percent_unlinked"]), expected_missing, abs_tol=1e-12)):
                raise ValueError("Invalid unlinked percentage")
    if len(annual) != len(YEARS) or {int(r["year"]) for r in annual} != set(YEARS):
        raise ValueError("Incomplete annual statistics")
    for row in annual:
        subset = [r for r in data if r["year"] == row["year"]]
        values = [float(r["percent_linked"]) for r in subset if r["percent_linked"] != ""]
        quantiles = statistics.quantiles(values, n=100, method="inclusive")
        wanted = {"mean_percent_linked": statistics.mean(values), "median_percent_linked": statistics.median(values),
                  **{f"p{p}_percent_linked": quantiles[p - 1] for p in [5, 10, 25, 75, 90, 95]}}
        for field, value in wanted.items():
            if not math.isclose(float(row[field]), value, abs_tol=1e-10):
                raise ValueError(f"Independent statistic check failed: {field}")
        for field in ["total_works", "unlinked_works", "linked_works"]:
            if int(row[field]) != sum(int(r[field]) for r in subset):
                raise ValueError(f"Annual total mismatch: {field}")
        if int(row["journals_with_works"]) != len(values) or int(row["zero_work_journals"]) != len(subset) - len(values):
            raise ValueError("Nonempty journal count mismatch")
        pooled = 100 * int(row["linked_works"]) / int(row["total_works"])
        if not math.isclose(float(row["pooled_percent_linked"]), pooled, abs_tol=1e-10):
            raise ValueError("Pooled percentage mismatch")
    values = [float(r["percent_linked"]) for r in data if r["percent_linked"] != ""]
    paths = [OUT / f"{STEM}.{ext}" for ext in ["png", "pdf", "svg"]]
    paths += [OUT / f"{STEM}_data.csv", OUT / "journal_year_counts.csv", OUT / "CAPTION_AND_RESULTS.txt"]
    histogram_verified = False
    if (OUT / f"{HIST_STEM}_data.csv").exists():
        bins = collection.read_csv(OUT / f"{HIST_STEM}_data.csv")
        if len(bins) != 60 or {(int(r["year"]), int(r["bin_lower_percent"])) for r in bins} != {(y, low) for y in YEARS for low in range(0, 100, 10)}:
            raise ValueError("Unexpected histogram bin grid")
        for row in bins:
            year, low, high = (int(row[k]) for k in ["year", "bin_lower_percent", "bin_upper_percent"])
            if high != low + 10 or row["bin_upper_inclusive"] != str(high == 100):
                raise ValueError("Invalid histogram interval")
            values_in_year = [float(r["percent_linked"]) for r in data if int(r["year"]) == year and r["percent_linked"] != ""]
            count = sum(low <= v < high or (high == 100 and v == 100) for v in values_in_year)
            if int(row["journal_count"]) != count or not math.isclose(float(row["percent_of_nonempty_journals"]), 100 * count / len(values_in_year), abs_tol=1e-10):
                raise ValueError("Histogram count or percentage mismatch")
            if int(row["journals_with_works"]) != len(values_in_year) or int(row["zero_work_journals_excluded"]) != 639 - len(values_in_year):
                raise ValueError("Histogram sample size mismatch")
        histogram_verified = True
        paths += [OUT / f"{HIST_STEM}_data.csv", OUT / "HISTOGRAM_CAPTION.txt"]
        paths += [OUT / f"{HIST_STEM}.{ext}" for ext in ["png", "pdf", "svg"]]
    collection.save(OUT / "verification_summary.json", {
        "verified_at_utc": collection.now(), "raw_response_counts_match_all_3834_rows": True,
        "annual_means_medians_and_percentiles_independently_match": True,
        "histogram_bin_counts_independently_match": histogram_verified,
        "verifier": "Python statistics.mean, median, quantiles(method=inclusive)",
        "zero_work_journal_years": len(data) - len(values),
        "all_journal_year_mean_percent_linked": statistics.mean(values),
        "pooled_work_weighted_percent_linked": 100 * sum(int(r["linked_works"]) for r in data) / sum(int(r["total_works"]) for r in data),
        "current_plotting_code_sha256": digest(Path(__file__)),
        "artifact_sha256": {p.name: digest(p) for p in paths}})
    print("Verified all 3,834 saved count rows and annual means, medians, percentiles and totals; no API calls.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["collect", "plot", "histograms", "verify"])
    parser.add_argument("--output-dir", type=Path, default=OUT,
                        help="Output/cache directory; default contains the released Figure 8 snapshot")
    args = parser.parse_args()
    OUT = args.output_dir.resolve()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.stage == "collect":
        collect()
    elif args.stage == "plot":
        plot()
    elif args.stage == "histograms":
        histograms()
    else:
        verify()
