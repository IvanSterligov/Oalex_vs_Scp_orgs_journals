"""Collect the approved 220 x 639 x 2020-2025 count grid, with resumable pages."""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
INPUTS = HERE
ROOT = HERE.parent
YEARS = list(range(2020, 2026))
SC_URL = "https://api.elsevier.com/content/search/scopus"
OA_URL = "https://api.openalex.org/"


class Halt(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def credentials(database=None):
    values = {}
    path = ROOT / "secrets.txt"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            m = re.match(r"\s*([A-Za-z_][A-Za-z_0-9]*)\s*[:=]\s*(.*?)\s*$", line)
            if m:
                v = m[2]
                values[m[1].lower()] = ast.literal_eval(v) if v.startswith(("'", '"')) else v
    oa = os.environ.get("OPENALEX_API_KEY") or values.get("openalex_api_key")
    env = os.environ.get("SCOPUS_API_KEYS") or os.environ.get("SCOPUS_API_KEY")
    sc = env.split(",") if env else [v for k, v in sorted(values.items()) if re.fullmatch(r"scopus_api_key\d*", k)]
    sc = list(dict.fromkeys(k.strip() for k in sc if k.strip()))
    if database != "scopus" and not oa:
        raise Halt("Missing OPENALEX_API_KEY")
    if database != "openalex" and not sc:
        raise Halt("Missing SCOPUS_API_KEYS")
    return oa.strip() if oa else None, sc


def prepare():
    # Released October snapshots replace dependencies on the private workspace.
    source_paths = [INPUTS / "journals.csv", INPUTS / "orgs.csv"]
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    orgs = read_csv(source_paths[1])
    journals = [r for r in read_csv(source_paths[0]) if r["should_include_20_25"] == "TRUE"]
    assert len(orgs) == 220 and len(journals) == 639
    assert len({r["openalex_id"] for r in orgs}) == len(orgs)
    for db in ["scopus", "openalex"]:
        ids = [r[f"{db}_source_ids"] for r in journals]
        assert len(set(ids)) == len(journals) and all("|" not in x for x in ids)
    manifest_path = HERE / "input_manifest.json"
    manifest = {"created_at": now(), "input_sha256": hashes, "organisations": 220, "journals": 639, "years": YEARS,
                "source_batch_size": 100, "scopus_page_size": 25, "openalex_page_size": 200}
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if old["input_sha256"] != hashes:
            raise Halt("Inputs changed since this run started")
    else:
        save(manifest_path, manifest)
        if HERE != INPUTS:
            write_csv(HERE / "orgs.csv", orgs, list(orgs[0]))
            write_csv(HERE / "journals.csv", journals, list(journals[0]))
    return orgs, journals


def compact(database, payload):
    if database == "openalex":
        m = payload.get("meta", {})
        return {"meta": {k: m[k] for k in ["count", "next_cursor", "groups_count", "cost_usd"] if k in m},
                "group_by": [{"key": g["key"], "count": g["count"]} for g in payload.get("group_by", [])]}
    s = payload.get("search-results", {})
    if "opensearch:totalResults" not in s:
        raise Halt("Scopus response lacks totalResults")
    return {"total": int(s["opensearch:totalResults"]),
            "records": [{"eid": e.get("eid"), "source": e.get("source-id"), "date": e.get("prism:coverDate")}
                        for e in s.get("entry", []) if e.get("eid")],
            "items_per_page": s.get("opensearch:itemsPerPage")}


class Client:
    """One shared limiter, compact page cache, quota reserves, no 429 retry."""
    def __init__(self, database, keys, rps=4, cap=None):
        self.database = database
        self.keys = keys
        self.reserve = 100 if database == "openalex" else 250
        self.rps = rps
        self.cap = cap
        self.calls = 0
        self.next_time = 0
        self.next_key = 0
        self.lock = threading.Lock()
        self.local = threading.local()
        self.halted = False
        self.cache = {}
        self.quotas = {}
        self.path = HERE / f"{database}_pages.jsonl"
        if self.path.exists():
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    item = json.loads(line)
                    if item["status"] == 200:
                        self.cache[item["cache_key"]] = item
                    if "headers" in item and item.get("key_number"):
                        self.quotas[item["key_number"]] = item["headers"]
        self.stop_path = HERE / f"{database}_stop.json"
        if self.stop_path.exists():
            raise Halt(f"Saved {database} stop condition requires review: {self.stop_path.name}")

    def stop(self, reason, headers=None):
        with self.lock:
            self.halted = True
            save(self.stop_path, {"time": now(), "reason": reason, "headers": headers or {}})
        raise Halt(f"{self.database}: {reason}")

    def request(self, data, key_number=None, fresh=False):
        method = "POST" if self.database == "openalex" else "GET"
        token = hashlib.sha256(json.dumps([method, data, key_number], sort_keys=True).encode()).hexdigest()
        if not fresh and token in self.cache:
            return self.cache[token]["payload"]
        for attempt in range(3):
            with self.lock:
                if self.halted:
                    raise Halt(f"{self.database} dispatch stopped")
                if self.cap is not None and self.calls >= self.cap:
                    self.halted = True
                    raise Halt("Pilot request cap reached")
                available = []
                for i in range(len(self.keys)):
                    h = self.quotas.get(i + 1, {})
                    remaining = h.get("x-ratelimit-remaining")
                    reset = h.get("x-ratelimit-reset")
                    # Scopus uses epoch reset; OpenAlex uses seconds until reset.
                    reset_passed = self.database == "scopus" and reset and float(reset) <= time.time()
                    if remaining is None or reset_passed or float(remaining) > self.reserve + 32:
                        available.append(i)
                if key_number is not None:
                    available = [i for i in available if i == key_number - 1]
                if not available:
                    self.halted = True
                    save(self.stop_path, {"time": now(), "reason": "All credential quota reserves reached", "quotas": self.quotas})
                    raise Halt(f"{self.database}: quota reserves reached")
                i = available[self.next_key % len(available)]
                self.next_key += 1
                delay = self.next_time - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self.next_time = time.monotonic() + 1 / self.rps
                self.calls += 1
            if not hasattr(self.local, "session"):
                self.local.session = requests.Session()
            headers = {"Accept": "application/json", "User-Agent": "STI-main-count-refresh/2026"}
            headers["Authorization" if self.database == "openalex" else "X-ELS-APIKey"] = (
                "Bearer " + self.keys[i] if self.database == "openalex" else self.keys[i])
            started = now()
            try:
                response = (self.local.session.post(OA_URL, json=data, headers=headers, timeout=45)
                            if self.database == "openalex" else
                            self.local.session.get(SC_URL, params=data, headers=headers, timeout=45))
            except requests.RequestException as exc:
                self.log({"cache_key": token, "time": started, "key_number": i + 1,
                          "status": "network_error", "attempt": attempt + 1, "request": data})
                if "10013" in str(exc) or attempt == 2:
                    self.stop("Transport failure (checkpoint retained)")
                time.sleep(2 ** (attempt + 1))
                continue
            h = {k.lower(): v for k, v in response.headers.items()
                 if k.lower().startswith("x-ratelimit") or k.lower() in ["retry-after", "x-els-status"]}
            try:
                p = response.json()
            except ValueError:
                p = {"non_json_response": True}
            item = {"cache_key": token, "time": started, "key_number": i + 1, "status": response.status_code,
                    "headers": h, "request": data, "elapsed_seconds": round(response.elapsed.total_seconds(), 3)}
            if response.status_code == 200:
                try:
                    item["payload"] = compact(self.database, p)
                except Halt:
                    self.log(dict(item, payload={"invalid_success_payload": True}))
                    self.stop("Invalid successful response payload", h)
            else:
                item["payload"] = p
            self.log(item)
            with self.lock:
                self.quotas[i + 1] = h
            if response.status_code == 429:
                self.stop("First HTTP 429; no retry", h)
            if response.status_code >= 500 and attempt < 2:
                retry = h.get("retry-after", "0")
                if not retry.isdigit() or int(retry) > 30:
                    self.stop("Server requested a long retry delay", h)
                time.sleep(max(int(retry), 2 ** (attempt + 1)))
                continue
            if response.status_code != 200:
                self.stop(f"HTTP {response.status_code}; no unchanged-request retry", h)
            return item["payload"]
        self.stop("Retry limit reached")

    def log(self, item):
        text = json.dumps(item, ensure_ascii=False)
        for k in self.keys:
            text = text.replace(k, "[REDACTED]")
        with self.lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")
                f.flush()
            if item["status"] == 200:
                self.cache[item["cache_key"]] = item


def sc_query(org, sources, years=YEARS):
    return (f"({org['scopus_lineage_query']}) AND PUBYEAR > {min(years)-1} AND PUBYEAR < {max(years)+1} AND ("
            + " OR ".join(f"SOURCE-ID({s})" for s in sources) + ")")


def sc_params(query, start=0, count=25):
    return {"query": query, "view": "STANDARD", "count": count, "start": start,
            "sort": "+coverDate,+publicationName,+creator"}


def tally(records, sources, years):
    seen = set()
    counts = Counter()
    for r in records:
        if not r["eid"] or r["eid"] in seen:
            raise Halt("Missing or duplicate EID in Scopus partition")
        seen.add(r["eid"])
        if r["source"] not in sources or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["date"] or ""):
            raise Halt("Unexpected source or missing/malformed Scopus coverDate")
        year = int(r["date"][:4])
        if year not in years:
            raise Halt("Scopus coverDate year outside queried PUBYEAR range")
        counts[r["source"], year] += 1
    return counts


def sc_partition(client, org, sources, years=YEARS, key_number=None):
    query = sc_query(org, sources, years)
    first = client.request(sc_params(query), key_number=key_number)
    total = first["total"]
    if total > 5000:
        parts = ([sc_partition(client, org, sources, [y], key_number) for y in years] if len(years) > 1 else
                 [sc_partition(client, org, sources[:len(sources)//2], years, key_number),
                  sc_partition(client, org, sources[len(sources)//2:], years, key_number)] if len(sources) > 1 else None)
        if parts is None:
            raise Halt("Single organisation/source/year exceeds retrieval limit; direct-count fallback required")
        counts = Counter()
        for p in parts:
            counts.update({(r["source"], r["year"]): r["count"] for r in p["counts"]})
        if sum(counts.values()) != total:
            raise Halt("Split Scopus partition total changed")
        return {"total": total, "counts": [{"source": s, "year": y, "count": n} for (s, y), n in counts.items()],
                "split": True, "completed_at": now()}
    records = list(first["records"])
    for start in range(25, total, 25):
        p = client.request(sc_params(query, start), key_number=key_number)
        if p["total"] != total:
            raise Halt("Scopus partition total changed between pages")
        records.extend(p["records"])
    if len(records) != total:
        raise Halt("Scopus retrieved record count does not match total")
    counts = tally(records, sources, years)
    return {"total": total, "counts": [{"source": s, "year": y, "count": n} for (s, y), n in counts.items()],
            "split": False, "completed_at": now()}


def oa_partition(client, org, sources, year):
    q = {"get_rows": "works", "filter_rows": [
        {"column_id": "authorships.institutions.lineage", "value": org["openalex_id"].split("/")[-1]},
        {"column_id": "publication_year", "value": year},
        {"join": "or", "filters": [{"column_id": "primary_location.source.id", "value": s} for s in sources]}],
        "group_by": [{"column_id": "primary_location.source.id"}]}
    cursor = "*"
    counts = {}
    total = None
    cursors = set()
    for _ in range(4):
        p = client.request({"oqo": q, "per_page": 200, "cursor": cursor})
        n = int(p["meta"]["count"])
        if total is not None and n != total:
            raise Halt("OpenAlex partition total changed between pages")
        total = n
        for g in p["group_by"]:
            s = g["key"].split("/")[-1]
            if s not in sources or s in counts:
                raise Halt("Unexpected or duplicate OpenAlex source bucket")
            counts[s] = int(g["count"])
        if sum(counts.values()) == total:
            return {"total": total, "counts": [{"source": s, "year": year, "count": n} for s, n in counts.items()],
                    "completed_at": now()}
        cursor = p["meta"].get("next_cursor")
        if not cursor or cursor in cursors or not p["group_by"]:
            raise Halt("OpenAlex group paging ended before totals matched")
        cursors.add(cursor)
    raise Halt("OpenAlex source buckets exceeded four pages")


def task_path(db, org_index, part):
    return HERE / "partitions" / db / f"{org_index:03d}_{part:02d}.json"


def sc_direct_fallback(client, org, sources, reason, key_number):
    """Use the original count method for metadata/paging exceptions only."""
    counts = []
    total = 0
    for sid in sources:
        n = client.request(sc_params(sc_query(org,[sid]),count=1),key_number=key_number)["total"]
        if n:
            annual = {y:client.request(sc_params(sc_query(org,[sid],[y]),count=1),key_number=key_number)["total"] for y in YEARS}
            if sum(annual.values()) != n:
                raise Halt("Direct fallback annual totals do not match source period total")
            counts.extend({"source":sid,"year":y,"count":v} for y,v in annual.items() if v)
        total += n
    expected = client.request(sc_params(sc_query(org,sources)),key_number=key_number)["total"]
    if total != expected:
        raise Halt("Direct fallback source totals do not match batch total")
    return {"total":total,"counts":counts,"completed_at":now(),"count_method":"direct_fallback","fallback_reason":reason}


def tasks(database, orgs, journals):
    sources = [j[f"{database}_source_ids"] for j in journals]
    for i, org in enumerate(orgs):
        parts = [(year, sources) for year in YEARS] if database == "openalex" else [(k, sources[k*100:(k+1)*100]) for k in range(7)]
        for part, ids in parts:
            yield i, org, part, ids


def run_task(database, client, task):
    i, org, part, ids = task
    p = task_path(database, i, part)
    if p.exists():
        return
    if database == "openalex":
        value = oa_partition(client, org, ids, part)
    else:
        key_number = 1+(i*7+part)%len(client.keys)
        try:
            value = sc_partition(client, org, ids, key_number=key_number)
            value["count_method"] = "record_tally"
        except Halt as exc:
            reason = str(exc)
            if not any(x in reason for x in ["Missing or duplicate EID", "coverDate", "source/year exceeds retrieval limit"]):
                raise
            print(json.dumps({"direct_fallback": True,"org_index":i,"batch":part,"reason":reason}),flush=True)
            value = sc_direct_fallback(client, org, ids, reason, key_number)
    value.update({"organisation_id": org["openalex_id"], "part": part, "source_ids": ids})
    save(p, value)


def pilot(orgs, journals, sc_keys):
    plan_path = HERE / "pilot_plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    else:
        # Reuse the actual three October pilot selections, not private old exports.
        plan = json.loads((INPUTS / "pilot_plan.json").read_text(encoding="utf-8"))
        save(plan_path, plan)
    used = sum(1 for _ in (HERE/"scopus_pages.jsonl").open(encoding="utf-8")) if (HERE/"scopus_pages.jsonl").exists() else 0
    client = Client("scopus", sc_keys, cap=100-used)
    cases = plan["partitions"]
    first = cases[0]
    ids = [j["scopus_source_ids"] for j in journals[first["batch"]*100:(first["batch"]+1)*100]]
    query = sc_query(orgs[first["org_index"]], ids)
    key_totals = []
    for n in range(1, len(sc_keys)+1):
        key_totals.append(client.request(sc_params(query, count=1), key_number=n, fresh=True)["total"])
    if len(set(key_totals)) != 1:
        raise Halt("Scopus keys return different counts/entitlements")
    checked = []
    for case in cases:
        i,b = case["org_index"],case["batch"]
        org = orgs[i]
        ids = [j["scopus_source_ids"] for j in journals[b*100:(b+1)*100]]
        run_task("scopus", client, (i,org,b,ids))
        result = json.loads(task_path("scopus",i,b).read_text(encoding="utf-8"))
        annual = {y:sum(r["count"] for r in result["counts"] if r["year"]==y) for y in YEARS}
        direct = {y:client.request(sc_params(sc_query(org,ids,[y]),count=1))["total"] for y in YEARS}
        if annual != direct:
            raise Halt("Pilot local years differ from direct Scopus PUBYEAR counts")
        # Validate all six years for the highest-volume individual source, including zeros.
        source_totals = Counter()
        for r in result["counts"]:
            source_totals[r["source"]] += r["count"]
        sid = source_totals.most_common(1)[0][0] if source_totals else ids[0]
        cells = {y:sum(r["count"] for r in result["counts"] if r["year"]==y and r["source"]==sid) for y in YEARS}
        cells_direct = {y:client.request(sc_params(sc_query(org,[sid],[y]),count=1))["total"] for y in YEARS}
        if cells != cells_direct:
            raise Halt("Pilot local source/year cells differ from direct Scopus counts")
        checked.append({**case,"organisation":org["openalex_display_name"],"total":result["total"],
                        "local_annual":annual,"direct_annual":direct,"checked_source":sid,
                        "local_cells":cells,"direct_cells":cells_direct})
        print(json.dumps({"pilot_partition":case,"total":result["total"],"requests_this_process":client.calls}),flush=True)
    save(HERE/"pilot_validation.json",{"completed_at":now(),"passed":True,"request_cap":100,"requests_total":used+client.calls,"requests_this_process":client.calls,
                                          "key_count":len(sc_keys),"key_totals":key_totals,"partitions":checked,"quotas":client.quotas})
    print("Scopus pilot passed",flush=True)


def collect(database, orgs, journals, keys, workers):
    if database == "scopus":
        p = HERE/"pilot_validation.json"
        if not p.exists() or not json.loads(p.read_text(encoding="utf-8"))["passed"]:
            raise Halt("Successful Scopus pilot required")
    client = Client(database, keys)
    all_tasks = list(tasks(database,orgs,journals))
    pending = [t for t in all_tasks if not task_path(database,t[0],t[2]).exists()]
    start = time.monotonic()
    done = len(all_tasks)-len(pending)
    print(json.dumps({"database":database,"completed":done,"partitions":len(all_tasks),"pending":len(pending)}),flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Only a few tasks are in flight; a stopped credential prevents new requests.
        iterator = iter(pending)
        futures = {}
        for t in list(next(iterator,None) for _ in range(workers)):
            if t is not None:futures[pool.submit(run_task,database,client,t)] = t
        while futures:
            f = next(as_completed(futures))
            task = futures.pop(f)
            try:
                f.result()
            except Exception:
                client.halted = True
                for other in futures:other.cancel()
                raise
            done += 1
            if done % 20 == 0 or done == len(all_tasks):
                print(json.dumps({"database":database,"completed":done,"partitions":len(all_tasks),
                                  "requests_this_process":client.calls,"elapsed_seconds":round(time.monotonic()-start)}),flush=True)
            t = next(iterator,None)
            if t is not None:futures[pool.submit(run_task,database,client,t)] = t
    save(HERE/f"{database}_run_summary.json",{"completed_at":now(),"partitions":done,"requests_this_process":client.calls,
                                                "elapsed_seconds":round(time.monotonic()-start),"quotas":client.quotas})


def export(orgs,journals):
    counts = {}
    summary = {}
    for database in ["openalex","scopus"]:
        totals = Counter()
        methods = Counter()
        n = 0
        for task in tasks(database,orgs,journals):
            i,org,part,ids = task
            p = task_path(database,i,part)
            if not p.exists():raise Halt(f"Incomplete {database}: {p.name}")
            result = json.loads(p.read_text(encoding="utf-8"))
            methods[result.get("count_method","record_tally" if database == "scopus" else "group_by")] += 1
            assert result["organisation_id"] == org["openalex_id"] and result["source_ids"] == ids
            assert sum(r["count"] for r in result["counts"]) == result["total"]
            for r in result["counts"]:
                key=(i,r["source"],r["year"])
                if key in counts.get(database,{}):raise Halt("Duplicate output cell")
                counts.setdefault(database,{})[key]=r["count"]
                totals[r["year"]]+=r["count"]
            n+=1
        summary[database]={"partitions":n,"annual_link_totals":dict(totals),"cells":len(orgs)*len(journals)*len(YEARS),"count_methods":dict(methods)}
    def rows():
        for i,org in enumerate(orgs):
            for j in journals:
                for y in YEARS:
                    yield {"organisation_id":org["openalex_id"],"organisation":org["openalex_display_name"],
                           "npi_journal_id":j["npi_journal_id"],"journal_title":j["journal_title"],"year":y,
                           "scopus_source_id":j["scopus_source_ids"],"openalex_source_id":j["openalex_source_ids"],
                           "scopus_count":counts.get("scopus",{}).get((i,j["scopus_source_ids"],y),0),
                           "openalex_count":counts.get("openalex",{}).get((i,j["openalex_source_ids"],y),0)}
    fields=["organisation_id","organisation","npi_journal_id","journal_title","year","scopus_source_id","openalex_source_id","scopus_count","openalex_count"]
    write_csv(HERE/"annual_counts.csv",rows(),fields)
    artifacts = [HERE / name for name in ("annual_counts.csv", "orgs.csv", "journals.csv", "pilot_validation.json",
                                         "openalex_counts.csv", "scopus_counts.csv") if (HERE / name).exists()]
    save(HERE/"validation_summary.json",{"verified_at":now(),"years":YEARS,"organisations":len(orgs),"journals":len(journals),
                                            "complete":True,"databases":summary,
                                            "combined_rows":len(orgs)*len(journals)*len(YEARS),
                                            "artifact_sha256":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in artifacts},
                                            "collector_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    print(json.dumps(summary),flush=True)


def export_matrix(database, orgs, journals):
    rows = {(i,y):{"year":y,"openalex_id":org["openalex_id"]} for i,org in enumerate(orgs) for y in YEARS}
    ids = [j[f"{database}_source_ids"] for j in journals]
    for task in tasks(database,orgs,journals):
        i,org,part,sources=task
        p=task_path(database,i,part)
        if not p.exists():raise Halt(f"Incomplete {database}: {p.name}")
        d=json.loads(p.read_text(encoding="utf-8"))
        assert d["organisation_id"]==org["openalex_id"] and d["source_ids"]==sources
        assert sum(r["count"] for r in d["counts"])==d["total"]
        for sid in sources:
            for y in ([part] if database=="openalex" else YEARS):rows[i,y][sid]=0
        for r in d["counts"]:rows[i,r["year"]][r["source"]]=r["count"]
    assert all(len(r)==len(ids)+2 for r in rows.values())
    write_csv(HERE/f"{database}_counts.csv",(rows[i,y] for y in YEARS for i in range(len(orgs))),["year","openalex_id"]+ids)
    print(json.dumps({"exported_database":database,"rows":len(rows),"journal_columns":len(ids)}),flush=True)


def status():
    for db in ["openalex","scopus"]:
        p=HERE/f"{db}_pages.jsonl"
        if not p.exists():continue
        entries=[]
        for line in p.read_text(encoding="utf-8").splitlines():
            try:entries.append(json.loads(line))
            except json.JSONDecodeError:continue  # An in-flight append can have a partial final line.
        records={}
        quotas={}
        for r in entries:
            quotas[r["key_number"]]=r.get("headers",{}).get("x-ratelimit-remaining")
            if db=="scopus" and r["status"]==200 and r["request"].get("count")==25 and r["request"].get("sort")=="+coverDate,+publicationName,+creator":
                records.setdefault(r["request"]["query"],set()).update(e["eid"] for e in r["payload"]["records"])
        files=list((HERE/"partitions"/db).glob("*.json"))
        print(json.dumps({"database":db,"last_time":entries[-1]["time"],"requests_logged":len(entries),"completed_partitions":len(files),
                          "retrieved_links":sum(map(len,records.values())) if db=="scopus" else None,
                          "fallbacks":sum(json.loads(f.read_text(encoding="utf-8")).get("count_method")=="direct_fallback" for f in files),
                          "responses_429":sum(r["status"]==429 for r in entries),"quota_remaining":quotas}),flush=True)


def main():
    global HERE
    parser=argparse.ArgumentParser()
    parser.add_argument("stage",choices=["prepare","pilot","openalex","scopus","export","export_openalex","export_scopus","status"])
    parser.add_argument("--workers",type=int,default=4)
    parser.add_argument("--run-dir",type=Path,default=HERE,
                        help="Output/cache directory; inputs always use the released October snapshots")
    args=parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    HERE=args.run_dir.resolve()
    HERE.mkdir(parents=True,exist_ok=True)
    orgs,journals=prepare()
    if args.stage=="prepare":return
    if args.stage=="status":return status()
    if args.stage=="export":return export(orgs,journals)
    if args.stage.startswith("export_"):return export_matrix(args.stage.split("_")[1],orgs,journals)
    oa,sc=credentials("scopus" if args.stage=="pilot" else args.stage)
    if args.stage=="pilot":return pilot(orgs,journals,sc)
    collect(args.stage,orgs,journals,[oa] if args.stage=="openalex" else sc,args.workers)


if __name__=="__main__":
    try:main()
    except Exception as exc:
        print(f"STOP: {type(exc).__name__}: {exc}",flush=True)
        raise SystemExit(1)
