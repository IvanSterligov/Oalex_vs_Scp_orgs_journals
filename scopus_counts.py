"""Collect Scopus institution-by-journal counts by publication year."""

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


API_URL = "https://api.elsevier.com/content/search/scopus"
DEFAULT_YEARS = "2020-2024"
_thread_local = threading.local()


class AllKeysExhausted(RuntimeError):
    """Raised when every supplied Scopus key has exhausted its weekly quota."""


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


class KeyManager:
    def __init__(self, keys: list[str]) -> None:
        clean = [key.strip() for key in keys if key.strip()]
        if not clean:
            raise ValueError("at least one Scopus API key is required")
        self.keys = clean
        self.exhausted: set[int] = set()
        self.active = 0
        self.lock = threading.Lock()

    def current(self) -> tuple[int, str]:
        with self.lock:
            available = [i for i in range(len(self.keys)) if i not in self.exhausted]
            if not available:
                raise AllKeysExhausted(
                    "All Scopus API keys are exhausted; rerun with --resume after quota reset"
                )
            if self.active not in available:
                self.active = available[0]
            return self.active, self.keys[self.active]

    def mark_exhausted(self, index: int) -> None:
        with self.lock:
            self.exhausted.add(index)
            available = [i for i in range(len(self.keys)) if i not in self.exhausted]
            if not available:
                raise AllKeysExhausted(
                    "All Scopus API keys are exhausted; rerun with --resume after quota reset"
                )
            self.active = available[0]
            print(f"Scopus key #{index + 1} exhausted; switched to key #{self.active + 1}.")


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


def _session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
    return _thread_local.session


class ScopusClient:
    def __init__(
        self,
        keys: list[str],
        requests_per_second: float = 5,
        timeout: float = 30,
        max_retries: int = 10,
    ) -> None:
        self.key_manager = KeyManager(keys)
        self.rate_limiter = RateLimiter(requests_per_second)
        self.timeout = timeout
        self.max_retries = max_retries

    def total(self, query: str, date_range: str | None = None) -> int:
        params = {"query": query, "start": 0, "count": 1}
        if date_range:
            params["date"] = date_range

        for attempt in range(self.max_retries):
            key_index, key = self.key_manager.current()
            headers = {
                "X-ELS-APIKey": key,
                "Accept": "application/json",
                "User-Agent": "openalex-scopus-replication/1.0",
            }
            self.rate_limiter.wait()
            try:
                response = _session().get(
                    API_URL,
                    headers=headers,
                    params=params,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if attempt == self.max_retries - 1:
                    raise RuntimeError("Scopus request failed after retries") from exc
                time.sleep(min(2**attempt, 60))
                continue

            if response.status_code == 429:
                els_status = response.headers.get("X-ELS-Status", "").upper()
                remaining = response.headers.get("X-RateLimit-Remaining")
                if "QUOTA" in els_status or remaining == "0":
                    self.key_manager.mark_exhausted(key_index)
                    continue
                if attempt == self.max_retries - 1:
                    response.raise_for_status()
                time.sleep(min(max(1, 2**attempt), 60))
                continue
            if response.status_code >= 500:
                if attempt == self.max_retries - 1:
                    response.raise_for_status()
                time.sleep(min(2**attempt, 60))
                continue

            response.raise_for_status()
            payload = response.json().get("search-results", {})
            if "opensearch:totalResults" not in payload:
                raise ValueError("Scopus response has no total-results field")
            return int(payload["opensearch:totalResults"])

        raise RuntimeError("Scopus request failed after retries")

    def counts(self, lineage_query: str, source_id: str, years: list[int]) -> dict[int, int]:
        source = str(source_id).strip()
        if not source.isdigit():
            raise ValueError(f"invalid Scopus source ID: {source_id!r}")
        base_query = f"({lineage_query.strip()}) AND SOURCE-ID({source})"
        date_range = str(years[0]) if len(years) == 1 else f"{years[0]}-{years[-1]}"
        total = self.total(base_query, date_range=date_range)
        if total == 0:
            return {year: 0 for year in years}
        counts = {
            year: self.total(f"{base_query} AND PUBYEAR = {year}")
            for year in years
        }
        if sum(counts.values()) != total:
            raise ValueError("Scopus annual counts do not sum to the range precheck")
        return counts


def load_inputs(
    org_path: Path,
    journal_path: Path,
    max_orgs: int | None = None,
    max_journals: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    orgs = pd.read_csv(org_path, dtype=str)
    journals = pd.read_csv(journal_path, dtype=str)
    required_org = {"openalex_id", "scopus_lineage_query"}
    required_journal = {"openalex_source_id", "scopus_source_id"}
    if not required_org.issubset(orgs.columns):
        raise ValueError(f"organisation file must contain {sorted(required_org)}")
    if not required_journal.issubset(journals.columns):
        raise ValueError(f"journal file must contain {sorted(required_journal)}")
    if orgs[list(required_org)].isna().any().any() or orgs.openalex_id.duplicated().any():
        raise ValueError("organisation IDs and lineage queries must be present; IDs must be unique")
    if journals[list(required_journal)].isna().any().any():
        raise ValueError("journal IDs must be present")
    if journals.openalex_source_id.duplicated().any() or journals.scopus_source_id.duplicated().any():
        raise ValueError("journal IDs must be unique")
    if not journals.scopus_source_id.map(str.isdigit).all():
        raise ValueError("scopus_source_id values must contain digits only")
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
    for journal in journals.itertuples(index=False):
        for org in orgs.itertuples(index=False):
            values = matrix.loc[(years, org.openalex_id), journal.openalex_source_id]
            if values.isna().any():
                yield (
                    org.openalex_id,
                    org.scopus_lineage_query,
                    journal.openalex_source_id,
                    journal.scopus_source_id,
                )


def collect(
    client: ScopusClient,
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
            institution, lineage, openalex_source, scopus_source = next(pair_iterator)
        except StopIteration:
            return False
        future = executor.submit(client.counts, lineage, scopus_source, years)
        futures[future] = (institution, openalex_source)
        return True

    for _ in range(min(len(pairs), workers * 2)):
        submit_one()

    completed = 0
    try:
        with tqdm(total=len(pairs), desc="Scopus pairs") as progress:
            while futures:
                future = next(as_completed(futures))
                institution, source = futures.pop(future)
                try:
                    result = future.result()
                    for year, count in result.items():
                        matrix.loc[(year, institution), source] = count
                except AllKeysExhausted as exc:
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
    parser.add_argument("--output", type=Path, default=Path("scopus_counts.tsv"))
    parser.add_argument("--years", default=DEFAULT_YEARS)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--requests-per-second", type=float, default=5)
    parser.add_argument("--checkpoint-every", type=int, default=250)
    parser.add_argument("--max-orgs", type=int)
    parser.add_argument("--max-journals", type=int)
    parser.add_argument("--resume", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    keys = [part.strip() for part in os.environ.get("SCOPUS_API_KEYS", "").split(",") if part.strip()]
    if not keys:
        raise SystemExit("Set SCOPUS_API_KEYS to one key or a comma-separated list.")
    years = parse_years(args.years)
    orgs, journals = load_inputs(args.orgs, args.journals, args.max_orgs, args.max_journals)
    client = ScopusClient(keys, requests_per_second=args.requests_per_second)
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
