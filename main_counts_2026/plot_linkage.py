"""Plot verified annual institutional counts; reads local files only."""

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


HERE = Path(__file__).resolve().parent
STEM = "figure_openalex_vs_scopus_by_year"


def annual_totals(database, summary):
    path = HERE / f"{database}_counts.csv"
    expected_hash = summary["artifact_sha256"][path.name]
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
        raise ValueError(f"{path.name} differs from the verified export")
    totals = defaultdict(int)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        sources = [name for name in reader.fieldnames if name not in {"year", "openalex_id"}]
        if len(sources) != summary["journals"]:
            raise ValueError("Unexpected journal column count")
        for row in reader:
            totals[int(row["year"])] += sum(int(row[source]) for source in sources)
    expected = {
        int(year): count
        for year, count in summary["databases"][database]["annual_link_totals"].items()
    }
    if dict(totals) != expected:
        raise ValueError(f"{database} annual totals differ from validation summary")
    return totals


def main():
    summary = json.loads((HERE / "validation_summary.json").read_text(encoding="utf-8"))
    if not summary["complete"]:
        raise ValueError("Collection is incomplete")
    scopus = annual_totals("scopus", summary)
    openalex = annual_totals("openalex", summary)
    years = summary["years"]
    percentages = [100 * openalex[year] / scopus[year] for year in years]
    with (HERE / f"{STEM}_data.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["year", "scopus_linked_count", "openalex_linked_count", "openalex_pct_scopus"])
        writer.writerows((year, scopus[year], openalex[year], value) for year, value in zip(years, percentages))

    plt.rcParams.update({"font.family": "Arial", "font.size": 12, "axes.titlesize": 18, "axes.labelsize": 13})
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(years, percentages, color="#059669", marker="o", markersize=8, linewidth=2.5,
            label="OpenAlex (2 Oct 2026 full data)")
    ax.axhline(100, color="#D97706", linewidth=1.5, linestyle="--", label="Scopus = 100%")
    for year, value in zip(years, percentages):
        ax.annotate(f"{value:.1f}%", (year, value), xytext=(0, 11), textcoords="offset points",
                    ha="center", fontsize=10, color="#047857")
    ax.set_title("OpenAlex institutional linkage relative to Scopus", loc="left", pad=16)
    ax.set_ylabel("OpenAlex linked counts as % of Scopus")
    ax.set_xticks(years)
    ax.set_ylim(65, 105)
    ax.set_yticks(range(65, 106, 5))
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#E5E7EB", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, ncol=2, loc="upper center", bbox_to_anchor=(0.5, -0.10))
    ax.text(0, -0.25,
            f"{summary['journals']} selected journals · {summary['organisations']} organisations · all document types\n"
            "Full institutional counts in both databases, collected 2 October 2026.",
            transform=ax.transAxes, fontsize=10, color="#4B5563", va="top")
    fig.subplots_adjust(left=0.10, right=0.98, top=0.89, bottom=0.27)
    for extension in ("png", "pdf"):
        fig.savefig(HERE / f"{STEM}.{extension}", dpi=300, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print("Year  OpenAlex / Scopus")
    for year, value in zip(years, percentages):
        print(f"{year}  {value:.4f}%")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=HERE)
    args = parser.parse_args()
    HERE = args.run_dir.resolve()
    main()
