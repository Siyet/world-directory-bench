# world-directory-bench

Sources and results of benchmark series for the
[msgspec-fastapi](https://github.com/Siyet/msgspec-fastapi) README: a small
reference-data HTTP service (countries, divisions, cities, airports) measured on FastAPI
with Pydantic models and with msgspec Structs and `MsgspecRouter`, plus other variants
(asyncpg without the ORM, uvloop, a Redis cache, Litestar and Go).

Two series are published. Series 3, run on 2026-10-14 on a cloud VM with 6 dedicated
vCPUs (AMD EPYC-Rome, KVM, Ubuntu 24.04.5), compares the variants of the service (see
Results). Series 8, run on 2026-11-18 on a different cloud VM, compares two uvicorn
settings of the optimized variant: the HTTP protocol implementation and the access log
(see Series 8). Series 4 to 7 are not published. The values of the two series are not
comparable with each other.

The top-level scripts are those of series 8. The repository as published with series 3,
whose files match `results/source-bindings.json`, is at tag `series-3`.

## Results (series 3)

Each row is a separate paired comparison of two variants (A and B) of the same API, 8
blocks per comparison; for Go, B is a separate implementation of the API. Requests per
second are the geometric means of each side, CPU time per response is the arithmetic
mean, and the changes are the paired estimates with a nominal 95% interval. The rows are
not additive. The series is flagged for host drift; see Method.

| Endpoint | Change (A → B) | Requests/s, A → B | Throughput change [95% interval] | CPU µs per response, A → B | CPU change [95% interval] |
|---|---|---|---|---|---|
| GET | Standard FastAPI → FastAPI + msgspec, asyncpg, uvloop | 515 → 1,162 | +125.5% [+123.4, +127.6] | 1,940 → 860 | -55.7% [-56.1, -55.2] |
| GET | Pydantic models → msgspec Structs (`MsgspecRouter`) | 514 → 569 | +10.7% [+9.9, +11.5] | 1,944 → 1,757 | -9.7% [-10.3, -9.0] |
| GET | Struct `response_model` → `response_model=None` | 566 → 566 | 0.0% [-1.0, +1.0] ¹ | 1,767 → 1,767 | 0.0% [-1.0, +1.0] ¹ |
| GET | Pydantic models → msgspec, `response_model=None` | 511 → 569 | +11.4% [+8.8, +14.0] | 1,959 → 1,758 | -10.2% [-12.3, -8.1] |
| GET | SQLAlchemy ORM → asyncpg queries | 568 → 1,152 | +102.7% [+100.8, +104.6] | 1,759 → 868 | -50.7% [-51.1, -50.2] |
| GET | asyncio → uvloop | 1,160 → 1,166 | +0.6% [-0.9, +2.1] ¹ | 862 → 857 | -0.5% [-2.0, +1.0] ¹ |
| GET | No cache → Redis view cache | 1,167 → 1,771 | +51.7% [+50.9, +52.6] | 856 → 564 | -34.1% [-34.5, -33.7] |
| GET | Cache values in JSON → MessagePack | 1,766 → 1,830 | +3.6% [+2.5, +4.8] | 566 → 546 | -3.5% [-4.6, -2.4] |
| GET | All optimizations without cache → standard FastAPI with the cache | 1,171 → 1,221 | +4.3% [+3.9, +4.8] | 854 → 819 | -4.1% [-4.6, -3.7] |
| GET | FastAPI + msgspec, asyncpg, uvloop → Litestar (same service, asyncpg, uvloop) | 1,165 → 1,409 | +20.9% [+19.6, +22.3] | 858 → 710 | -17.3% [-18.2, -16.4] |
| GET | FastAPI + msgspec, asyncpg, uvloop → Go (net/http, pgx) | 1,169 → 4,204 | +259.5% [+256.0, +262.9] | 855 → 236 | -72.4% [-72.7, -72.2] |
| POST search | Pydantic models → msgspec Structs (`MsgspecRouter`) | 447 → 505 | +12.9% [+12.1, +13.7] | 2,237 → 1,981 | -11.4% [-12.1, -10.8] |
| POST search | Standard FastAPI → FastAPI + msgspec, asyncpg, uvloop | 445 → 1,172 | +163.3% [+160.8, +165.8] | 2,246 → 853 | -62.0% [-62.4, -61.7] |
| POST validate | Pydantic models → msgspec Structs (`MsgspecRouter`) | 1,610 → 2,703 | +67.9% [+67.0, +68.7] | 621 → 370 | -40.4% [-40.7, -40.1] |

¹ The interval includes zero, so no difference was detected.

- Standard FastAPI: Pydantic models, SQLAlchemy ORM, asyncio. "FastAPI + msgspec,
  asyncpg, uvloop" is `MsgspecRouter` with `response_model=None`, asyncpg queries and
  uvloop; it is also the A side of the "No cache", "All optimizations without cache",
  Litestar and Go rows.
- The rows from "Pydantic models → msgspec Structs" to "No cache → Redis view cache" form
  a chain in which each step, apart from the combined Pydantic → `response_model=None`
  row, changes one setting. The MessagePack row stores MessagePack bytes in Redis and
  decodes them with msgspec, instead of JSON with `json.loads`, a repr check and
  `msgspec.convert`.
- In the Litestar row, the same service runs on Litestar with hand-parsed query
  parameters. The Go variant uses net/http, database/sql with pgx and `encoding/json`.
  These rows are not router comparisons, and Litestar and Go are not compared with each
  other. Load generator CPU per response is lower with Go, so the Go throughput includes
  a client-side effect.
- GET requests: 9 of 10 go to the same page, `/cities?limit=100`, and every 10th to a
  page after a random city id (`after_id`) that is not repeated within the run. With the
  Redis view cache, about 90% of GET requests are cache hits; without it, PostgreSQL is
  queried for every request.
- In POST search, small JSON filter bodies are sent to `/cities/search`. In POST
  validate, a JSON body of 100 cities (about 23 KB) is sent to `/cities/validate`, where
  it is only decoded and validated, without database access.
- Throughput is measured with 20 closed-loop clients at saturation of the single API
  core. The ratios are throughput ratios under this load, not capacity ratios, and the
  CPU columns mirror the throughput columns rather than being independent evidence.
- The cache rows depend on the request mix (about 90% hits on one hot page) and on
  concurrency: with 20 clients, concurrent hits of the hot page share one Redis read and,
  for the JSON cache, one `json.loads` and repr check (Cashews `protected=True`);
  validation, including the MessagePack decode, is done for every response. At a fixed
  800 requests per second (`equal_load` in `results/summary.json`), API CPU per response
  was about 1,080 µs without the cache, 1,140 µs with the JSON cache, 845 µs with the
  MessagePack cache and 1,240 µs for standard FastAPI with the cache, which used 0.995 of
  the API core at that rate. At this rate the JSON cache did not reduce CPU per response,
  and standard FastAPI with the cache used more CPU per response than all optimizations
  without the cache. So these results do not show that adding the cache to standard
  FastAPI outperforms the optimizations (`claim_scope_cache_vs_optimizations`).

## Series 8: uvicorn HTTP protocol and access log

Series 8 measures the configuration of the optimized variant above (`MsgspecRouter`
with `response_model=None`, asyncpg queries, uvloop, one uvicorn worker, no cache), with
the router from msgspec-fastapi main instead of the vendored copy, and with two uvicorn
settings changed one at a time: `--http h11` versus `--http httptools`, and the access
log off (`--no-access-log`) versus on (the uvicorn default). The request mix, the load
generator (the same source and executables), the paired design and the estimator are
those of series 3 (see Method), as carried into the unpublished series 7, to which
`run.py` and the protocol refer. The differences are listed below.

| Change (A → B) | Requests/s, A → B | Throughput change [95% interval] | API CPU µs per response, A → B | CPU change [95% interval] |
|---|---|---|---|---|
| `--http h11` → `--http httptools` | 744 → 825 | +10.9% [+10.2, +11.5] | 1,343 → 1,211 | -9.8% [-10.3, -9.3] |
| Access log off → on | 750 → 706 | -5.9% [-6.3, -5.5] | 1,333 → 1,417 | +6.3% [+5.8, +6.7] |
| A/A control (same variant on both sides) | 747 → 750 | +0.4% [-0.3, +1.1] ¹ | 1,338 → 1,333 | -0.4% [-1.1, +0.3] ¹ |

¹ The interval includes zero, so no difference was detected.

- The access log is written by the `uvicorn.access` logger to the container stdout and
  collected by the Docker `json-file` logging driver. That collection runs in the
  containerd shim of the API container and in dockerd, outside the API container and
  on other CPUs, so it is not included in the API CPU column. The CPU of these two
  processes per response rose by 54 µs [54, 55] with the log on (about 23 µs in the
  shim and 31 µs in dockerd), and the CPU per response of the API and these two
  processes together rose by about 138 µs [132, 143]. Both values are the mean of the
  per-block differences B minus A in µs, with a 95% t interval over the 8 blocks,
  computed from `points` in `results/series8/summary.json`. Other log destinations
  were not measured.
- With the log on, uvicorn formats an access log line for every response. A log that is
  configured but filtered by level was not measured.
- The API image contains httptools, but uvicorn 0.54.0 with `--http h11` does not import
  it; this is checked in every run through the files mapped by the API process. Before
  the series, the presence or absence of the access log lines was checked for each
  variant.
- At a fixed 400 requests per second (`equal_load` in `results/series8/summary.json`,
  two runs per variant, no interval), API CPU per response was about 1,480 µs with h11,
  1,370 µs with httptools and 1,590 µs with h11 and the access log; the two log
  processes used about 57 µs per response with the log on and under 1 µs without it.
- Memory (RSS of the API process) did not change detectably in either comparison.
- The rows are not additive. Versions: Python 3.14.10, uvicorn 0.54.0, msgspec-fastapi
  at the commit recorded in `results/series8/router-main.json`.

Differences from series 3:

- Host: a cloud KVM VM with 8 vCPUs in two NUMA nodes (Intel Xeon, Icelake, Debian 12).
  Dedicated physical cores are not confirmed for this VM. The containers run on CPUs 0
  to 3 (NUMA node 0; API on CPU 0), and Docker, containerd, user sessions, unbound
  kernel work queues and the NIC interrupts on CPUs 4 to 7 (`isolate.sh`). The highest
  steal time of any run was 0.1% of a CPU.
- Static controls after the series were 1.2% faster than before it, below the drift
  flag (`host_drift`).
- Only GET pages are measured. The Pydantic and Go variants run only as references: to
  record the pages of the cold pool and, before the series, to check the responses of
  the measured variants.

`results/series8` contains the analysed results (`summary.json`), the protocol written
before the main series, the source bindings (keys with the server prefix
`/srv/talk/wdb-s8/`), the host environment (host name removed), the record of the API
image and the record of the msgspec-fastapi build. The loaded data are the same files as
in series 3 (`results/directory.load.json`).

## Data

- Sources: the GeoNames `cities500` export with country and administrative codes
  (https://www.geonames.org/, licensed under
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)) and the OurAirports data
  (https://ourairports.com/, public domain), downloaded on 2026-10-05.
  `data/source-manifest.json` lists the URLs, sizes and SHA-256 of the downloaded files.
- The files are downloaded, with their checksums recorded, by `data/fetch.py` and
  normalized by `data/prepare.py` into JSONL files for the four resources plus a file
  with the provenance of the city-to-division links. `data/normalized-report.json`
  contains the counts, exclusions and unresolved references.
- The source and normalized data files are not included. GeoNames is updated
  continuously, so a new download gives a newer snapshot than the one measured.
- The JSON files in `bench` (request cases and the static response body) contain city
  records derived from GeoNames (CC BY 4.0) and normalized by `data/prepare.py`.
- Loaded into the database: 501 country records and 55,246 division records (the
  GeoNames and OurAirports lists are kept separate), 224,578 cities and 86,185
  airports. The database is about 231 MB including indexes
  (`results/directory.db_size.json`). `results/directory.load.json` has the SHA-256 of
  the loaded files.

## Database access

- In both series, PostgreSQL 18.6 in Docker (the same image ID), with its data directory
  on tmpfs (in memory), limited to 0.75 CPU on a core shared only with Redis and the
  telemetry sampler, `shared_buffers=64MB`.
- In series 3, in the Python variants compared in the msgspec-fastapi README, the
  database is accessed through the SQLAlchemy 2.1.3 ORM with the asyncpg 0.31.0 driver
  (`DB_BACKEND=orm`). In the `orm-asyncpg` comparison, the ORM is replaced by asyncpg
  queries (`bench/service/asyncpg_repository.py`).
- No cache is used in the msgspec-fastapi comparisons (`CACHE_MODE=bypass`).

## Layout

- `run.py`: the series 8 runner. Its stages, in order: `build`, `setup`, `cold`,
  `smoke`, `protocol`, `preflight`, `static_pre`, `main`, `post`, `cleanup`. The summary
  published as `results/series8/summary.json` is computed with `analyze.py`; the resource
  use of the containers, of the log collection processes and of the host is sampled with
  `telemetry.py` and `host_telemetry.py`. `matched-rate-rule.md` is the rule that sets
  the fixed request rate for the equal-load runs, and `isolate.sh` sets the CPU isolation
  of the host (new in series 8). The series 3 versions of the other files are at tag
  `series-3`.
- `bench/service/Dockerfile-T`, `bench/service/requirements-T.in` and
  `requirements-T.lock`: the API image of series 8. `bench/router-main-src` is the
  msgspec-fastapi source (`bench/router-main-src.json` records the commit and file
  hashes); the `build` stage builds it into a wheel installed in `bench/router-main-site`,
  which is mounted into the API container. `bench/router-main-shim` is a
  `fastapi_extension_msgspec` package that takes the place of the vendored router copy
  and re-exports `msgspec_fastapi`.
- `bench/service`: the FastAPI service. `fastapi_extension_msgspec` is the router copy
  used in series 3; on the request path it is the same code as msgspec-fastapi 0.0.1.
  The `country_code` pattern was removed before series 3, so the
  `country_code=zz` case of `test_invalid_query` in `tests/test_contract.py` fails.
- `bench/go-service`, `bench/apps`: the Go and Litestar variants.
- `generator`: the Go load generator used in both series (closed-loop clients, every
  response body checked). `bench/generator-source` is an earlier version that is
  recorded in the series bindings too.
- `bench/run.py`, `bench/setup.py`, `bench/analyze.py`, `bench/sample.py`: scripts of an
  earlier series, kept because the bindings cover the whole `bench` directory; they are
  not called from `run.py`. `bench/cache_control.py` is a separate check of the view
  cache, changed after series 3 (TTL and forced expiry checks) and not run in series 8.
- `bench/*.json`: request cases (the hot GET page, search bodies, the 100-city ingest
  body) and `body.json`, the response body of the static control server.
- `results`: series 3: the analysed results (`summary.json`), the protocol written before
  the main series, the source bindings and the host environment (host name removed). The
  msgspec-fastapi README rows are `ingest-pyd-msgspec`, `pyd-msgspec-schema` and
  `post-pyd-msgspec-schema` under `comparisons` in `results/summary.json`, and the
  `response_model=None` check is `schema-none`.

At tag `series-3`, all included files under `bench` and `generator`, the top-level
scripts and `matched-rate-rule.md` have the SHA-256 recorded in
`results/source-bindings.json` (the keys carry the server prefix
`/srv/talk/talk-srv-s3-20261014/`). The files used by series 8 have the SHA-256 recorded
in `results/series8/source-bindings.json`, except `isolate.sh`. Of the files listed
there, `evidence/image-T.json` and `evidence/router-main.json` are included as
`results/series8/image-T.json` and `router-main.json`; not included are the built
executables, the installed msgspec-fastapi and regexrs-py packages
(`bench/router-main-site`, recorded in `results/series8/router-main.json`) and the
generated cold pages. Files listed in the series 3 bindings but not included:

- `bench/site`, the packages added to `PYTHONPATH` in addition to
  `bench/service/requirements.lock`: litestar 2.24.0, litestar-htmx 0.5.0, polyfactory
  3.3.0, faker 40.43.0, rich 15.0.0, rich-click 1.9.9, multidict 7.1.0, multipart 2.0.1,
  pyyaml 6.0.3, sniffio 1.3.1, markdown-it-py 4.2.0 and mdurl 0.1.2, plus msgspec 0.22.0
  and uvloop 0.23.0, the same versions as in the lock;
- the built binaries (`bin`, `bench/bin`). The load generator and the Go service can be
  built from `generator` and `bench/go-service`; the source of the static control server
  (`bench/bin/static`) is not included;
- `bench/cold-cases.json` and `evidence/cold-pool.json`, which the `cold` stage
  generates;
- `bench/service/.pytest_cache`;
- `bench/service/README.md` and `bench/go-service/README.md`, development notes written
  before series 3 that do not describe it.

## Method (series 3)

Each comparison is 8 blocks in ABBA or BAAB order, one block of every comparison per
round. Every run starts a fresh API container pinned to one core, with a 30 s warmup and
a 60 s measurement under 20 closed-loop clients. The Python variants run as one uvicorn
worker with h11, on asyncio unless the variant uses uvloop; the Go variant runs with
`GOMAXPROCS=1`. Estimates are computed from the mean of the per-block log ratios with a
nominal 95% t interval over the 8 blocks. A static control after the series was about 10%
slower than before it (`host_drift` in `results/summary.json`). In the per-point values
(`points` of each comparison in `results/summary.json`), the mean signed deviation of
throughput from each variant's median is within 1% in rounds 0 to 6 and -1.4% in the last
round.

## Reproducing

The scripts were written for the hosts described above; running them elsewhere requires
changes. For series 8 (the top-level scripts):

- In `run.py`, PostgreSQL 18.6, Redis 7.4.11 and Debian 12 slim are given as image IDs
  of Docker Engine with the containerd image store, and the API image is built by the
  `build` stage from `bench/service/Dockerfile-T`. Pull equivalent images and replace
  the IDs if they differ.
- In `run.py`, the containers are pinned to CPUs 0 to 3 (API 0; PostgreSQL, Redis and
  the sampler 1; generator 2 and 3). At the start and end of the `main` and `post`
  stages, the run stops unless NUMA node 0 is CPUs 0 to 3, the runner process, Docker,
  containerd, user sessions and `init.scope` are limited to CPUs 4 to 7, no user-space
  process outside the containers is allowed on CPUs 0 to 3, the listed systemd timers
  are stopped, the unbound kernel work queues and the `virtio0` NIC interrupts are on CPUs
  4 to 7 and no NIC interrupt was handled on CPUs 0 to 3. `isolate.sh`, run after each
  boot, sets up the CPU limits, timers, work queues and interrupts. The normalized data
  are read from `data/normalized` or the path in `WDB_DATA`, and the built executables
  are run.
- The raw per-run measurements used as input to `analyze.py` are not included, so
  `results/summary.json` and `results/series8/summary.json` cannot be recomputed from
  this repository alone.
