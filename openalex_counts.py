"""Collect OpenAlex institution-by-journal counts by publication year."""

from __future__ import annotations

import argparse
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm


API_URL = "https://api.openalex.org/works"
DEFAULT_YEARS = "2020-2024"
_thread_local = threading.local()


class BudgetExhausted(RuntimeError):
    """Raised when the OpenAlex daily API budget is exhausted."""


class RateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.interval = 1.0 / requests_per_second
        self.lock = threading.Lock()
        self.next_request = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = self.next_request - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self.next_request = max(now, self.next_request) + self.interval


def parse_years(value: str) -> list[int]:
    value = value.strip()
    if "-" in value and "," not in value:
        start, end = (int(part.strip()) for part in value.split("-", 1))
        if end < start:
            raise ValueError("year range must be ascending")
        return list(range(start, end + 1))
    years = [int(part.strip()) for part in value.split(",") if part.strip()]
    if not years:
        raise ValueError("at least one year is required")
    years = sorted(set(years))
    if years != list(range(years[0], years[-1] + 1)):
        raise ValueError("years must form one continuous range")
    return years


def short_openalex_id(value: str, prefix: str) -> str:
    short = str(value).strip().rstrip("/").rsplit("/", 1)[-1]
    if not short.upper().startswith(prefix):
        raise ValueError(f"expected an OpenAlex {prefix} ID, got {value!r}")
    return short.upper()


def parse_group_counts(payload: dict, years: list[int]) -> dict[int, int]:
    counts = {year: 0 for year in years}
    groups = payload.get("group_by")
    if not isinstance(groups, list):
        raise ValueError("OpenAlex response has no group_by list")
    for group in groups:
        year = int(group["key"])
        if year in counts:
            counts[year] = int(group["count"])
    meta_count = payload.get("meta", {}).get("count")
    if meta_count is not None and sum(counts.values()) != int(meta_count):
        raise ValueError("OpenAlex grouped counts do not sum to meta.count")
    return counts


def _session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
    return _thread_local.session


class OpenAlexClient:
    def __init__(
        self,
        api_key: str,
        requests_per_second: float = 20,
        timeout: float = 30,
        max_retries: int = 8,
    ) -> None:
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "openalex-scopus-replication/1.0",
        }
        self.timeout = timeout
        self.max_retries = max_retries
        self.rate_limiter = RateLimiter(requests_per_second)

    def counts(self, institution_id: str, source_id: str, years: list[int]) -> dict[int, int]:
        institution = short_openalex_id(institution_id, "I")
        source = short_openalex_id(source_id, "S")
        year_filter = (
            str(years[0]) if len(years) == 1 else f"{years[0]}-{years[-1]}"
        )
        params = {
            "filter": (
                f"authorships.institutions.lineage:{institution},"
                f"primary_location.source.id:{source},"
                f"publication_year:{year_filter}"
            ),
            "group_by": "publication_year",
        }

        for attempt in range(self.max_retries):
            self.rate_limiter.wait()
            try:
                response = _session().get(
                    API_URL,
                    headers=self.headers,
                    params=params,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if attempt == self.max_retries - 1:
                    raise RuntimeError("OpenAlex request failed after retries") from exc
                time.sleep(min(2**attempt, 60))
                continue

            if response.status_code == 429:
                if response.headers.get("X-RateLimit-Remaining") == "0":
                    raise BudgetExhausted(
                        "OpenAlex daily API budget is exhausted; rerun with --resume after reset"
                    )
                if attempt == self.max_retries - 1:
                    response.raise_for_status()
                time.sleep(min(2**attempt, 60))
                continue
            if response.status_code >= 500:
                if attempt == self.max_retries - 1:
                    response.raise_for_status()
                time.sleep(min(2**attempt, 60))
                continue

            response.raise_for_status()
            return parse_group_counts(response.json(), years)

        raise RuntimeError("OpenAlex request failed after retries")


def load_inputs(
    org_path: Path,
    journal_path: Path,
    max_orgs: int | None = None,
    max_journals: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    orgs = pd.read_csv(org_path, dtype=str)
    journals = pd.read_csv(journal_path, dtype=str)
    required_org = {"openalex_id"}
    required_journal = {"openalex_source_id"}
    if not required_org.issubset(orgs.columns):
        raise ValueError(f"organisation file must contain {sorted(required_org)}")
    if not required_journal.issubset(journals.columns):
        raise ValueError(f"journal file must contain {sorted(required_journal)}")
    if orgs.openalex_id.isna().any() or orgs.openalex_id.duplicated().any():
        raise ValueError("openalex_id values must be present and unique")
    if journals.openalex_source_id.isna().any() or journals.openalex_source_id.duplicated().any():
        raise ValueError("openalex_source_id values must be present and unique")
    for value in orgs.openalex_id:
        short_openalex_id(value, "I")
    for value in journals.openalex_source_id:
        short_openalex_id(value, "S")
    return orgs.head(max_orgs), journals.head(max_journals)


def empty_matrix(orgs: pd.DataFrame, journals: pd.DataFrame, years: list[int]) -> pd.DataFrame:
    index = pd.MultiIndex.from_product(
        [years, orgs.openalex_id.tolist()], names=["year", "openalex_id"]
    )
    return pd.DataFrame(
        pd.NA,
        index=index,
        columns=journals.openalex_source_id.tolist(),
        dtype="Int64",
    )


def load_resume(path: Path, matrix: pd.DataFrame) -> pd.DataFrame:
    previous = pd.read_csv(path, sep="\t", dtype={"openalex_id": str})
    if not {"year", "openalex_id"}.issubset(previous.columns):
        raise ValueError("resume file must contain year and openalex_id")
    previous = previous.set_index(["year", "openalex_id"])
    common_index = matrix.index.intersection(previous.index)
    common_columns = matrix.columns.intersection(previous.columns)
    for column in common_columns:
        values = pd.to_numeric(previous.loc[common_index, column], errors="coerce").astype("Int64")
        matrix.loc[common_index, column] = values
    return matrix


def save_matrix(matrix: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    matrix.reset_index().to_csv(temporary, sep="\t", index=False, encoding="utf-8")
    os.replace(temporary, path)


def pending_pairs(matrix: pd.DataFrame, orgs: pd.DataFrame, journals: pd.DataFrame, years: list[int]):
    for source_id in journals.openalex_source_id:
        for institution_id in orgs.openalex_id:
            values = matrix.loc[(years, institution_id), source_id]
            if values.isna().any():
                yield institution_id, source_id


def collect(
    client: OpenAlexClient,
    orgs: pd.DataFrame,
    journals: pd.DataFrame,
    years: list[int],
    output: Path,
    resume: bool,
    workers: int,
    checkpoint_every: int,
) -> int:
    matrix = empty_matrix(orgs, journals, years)
    if output.exists():
        if not resume:
            raise FileExistsError(f"{output} exists; use --resume or choose another output")
        matrix = load_resume(output, matrix)

    pairs = list(pending_pairs(matrix, orgs, journals, years))
    failures: list[tuple[str, str, str]] = []
    fatal: BaseException | None = None
    if workers <= 0 or checkpoint_every <= 0:
        raise ValueError("workers and checkpoint_every must be positive")

    pair_iterator = iter(pairs)
    executor = ThreadPoolExecutor(max_workers=workers)
    futures = {}

    def submit_one() -> bool:
        try:
            institution, source = next(pair_iterator)
        except StopIteration:
            return False
        future = executor.submit(client.counts, institution, source, years)
        futures[future] = (institution, source)
        return True

    for _ in range(min(len(pairs), workers * 2)):
        submit_one()

    completed = 0
    try:
        with tqdm(total=len(pairs), desc="OpenAlex pairs") as progress:
            while futures:
                future = next(as_completed(futures))
                institution, source = futures.pop(future)
                try:
                    result = future.result()
                    for year, count in result.items():
                        matrix.loc[(year, institution), source] = count
                except BudgetExhausted as exc:
                    fatal = exc
                except Exception as exc:  # keep the cell missing so --resume retries it
                    failures.append((institution, source, type(exc).__name__))
                completed += 1
                progress.update(1)
                if completed % checkpoint_every == 0:
                    save_matrix(matrix, output)
                if fatal is not None:
                    for queued in futures:
                        queued.cancel()
                    break
                submit_one()
    finally:
        executor.shutdown(wait=True, cancel_futures=fatal is not None)
        save_matrix(matrix, output)

    if fatal is not None:
        raise fatal

    if failures:
        print(f"Completed with {len(failures)} failed pairs; rerun with --resume.")
        return 2
    print(f"Saved {output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orgs", type=Path, default=Path("data/orgs.csv"))
    parser.add_argument("--journals", type=Path, default=Path("data/journals.csv"))
    parser.add_argument("--output", type=Path, default=Path("openalex_counts.tsv"))
    parser.add_argument("--years", default=DEFAULT_YEARS)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--requests-per-second", type=float, default=20)
    parser.add_argument("--checkpoint-every", type=int, default=500)
    parser.add_argument("--max-orgs", type=int)
    parser.add_argument("--max-journals", type=int)
    parser.add_argument("--resume", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    api_key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("Set the OPENALEX_API_KEY environment variable.")
    years = parse_years(args.years)
    orgs, journals = load_inputs(args.orgs, args.journals, args.max_orgs, args.max_journals)
    client = OpenAlexClient(api_key, requests_per_second=args.requests_per_second)
    return collect(
        client,
        orgs,
        journals,
        years,
        args.output,
        args.resume,
        args.workers,
        args.checkpoint_every,
    )


if __name__ == "__main__":
    raise SystemExit(main())
