# world-directory-bench

Sources and results of the benchmark series behind the
[msgspec-fastapi](https://github.com/Siyet/msgspec-fastapi) README: a small
reference-data HTTP service (countries, divisions, cities, airports) measured on FastAPI
with Pydantic models and with msgspec Structs and `MsgspecRouter`, plus other variants
(asyncpg without the ORM, uvloop, a Redis cache, Litestar and Go).

The results are from series 3 (earlier series are not published), run on 2026-10-14 on
a cloud VM with 6 dedicated vCPUs (AMD EPYC-Rome, KVM, Ubuntu 24.04.5).

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

- PostgreSQL 18.6 in Docker, with its data directory on tmpfs (in memory), limited to
  0.75 CPU on a core shared only with Redis and the telemetry sampler,
  `shared_buffers=64MB`.
- In the Python variants compared in the msgspec-fastapi README, the database is accessed
  through the SQLAlchemy 2.1.3 ORM with the asyncpg 0.31.0 driver (`DB_BACKEND=orm`). In
  the `orm-asyncpg` comparison, the ORM is replaced by asyncpg queries
  (`bench/service/asyncpg_repository.py`).
- No cache is used in the msgspec-fastapi comparisons (`CACHE_MODE=bypass`).

## Layout

- `run.py`: the series runner. Its stages, in order: `setup`, `cold`, `postcases`,
  `smoke`, `protocol`, `preflight`, `static_pre`, `main`, `post`, `cleanup`. The summary
  published as `results/summary.json` is computed with `analyze.py`; the resource use of
  the containers and the host is sampled with `telemetry.py` and `host_telemetry.py`.
  `matched-rate-rule.md` is the rule that sets the fixed request rate for the equal-load
  comparisons.
- `bench/service`: the FastAPI service. `fastapi_extension_msgspec` is the router copy
  used in the series; on the request path it is the same code as msgspec-fastapi 0.0.1.
  The `country_code` pattern was removed before these measurements, so the
  `country_code=zz` case of `test_invalid_query` in `tests/test_contract.py` fails.
- `bench/go-service`, `bench/apps`: the Go and Litestar variants.
- `generator`: the Go load generator used in this series (closed-loop clients, every
  response body checked). `bench/generator-source` is an earlier version that is
  recorded in the series bindings too.
- `bench/run.py`, `bench/setup.py`, `bench/analyze.py`, `bench/sample.py`: scripts of an
  earlier series, kept because the bindings cover the whole `bench` directory; they are
  not called from `run.py`. `bench/cache_control.py` is a separate check of the view
  cache.
- `bench/*.json`: request cases (the hot GET page, search bodies, the 100-city ingest
  body) and `body.json`, the response body of the static control server.
- `results`: the analysed results (`summary.json`), the protocol written before the
  main series, the source bindings and the host environment (host name removed). The
  msgspec-fastapi README rows are `ingest-pyd-msgspec`, `pyd-msgspec-schema` and
  `post-pyd-msgspec-schema` under `comparisons` in `results/summary.json`, and the
  `response_model=None` check is `schema-none`.

All included files under `bench` and `generator`, the top-level scripts and
`matched-rate-rule.md` have the SHA-256 recorded in `results/source-bindings.json` (the
keys carry the server prefix `/srv/talk/talk-srv-s3-20261014/`). Files listed in the
bindings but not included:

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
  before this series that do not describe it.

## Method

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

The scripts were written for the server described above; running them elsewhere
requires changes:

- In `run.py`, Docker images are given as local image IDs: a Python 3.14.7 image,
  PostgreSQL 18.6, Redis 7.4.11 and Debian 12 slim. Build or pull equivalent images and
  replace the IDs.
- In `run.py`, the containers are pinned to CPUs 0 to 3 (API 0; PostgreSQL, Redis and
  the sampler 1; generator 2 and 3). At the start and end of the `main` and `post`
  stages, the run stops unless the runner process, Docker, containerd, user sessions
  and `init.scope` are limited to CPUs 4 and 5, no process outside the containers is
  allowed on CPUs 0 to 3, the listed systemd timers are stopped and the `virtio0` NIC
  interrupts are on CPU 4 or 5. The normalized data are read from the path set in
  `run.py`, and the built binaries are executed.
- The raw per-run measurements used as input to `analyze.py` are not included, so
  `results/summary.json` cannot be recomputed from this repository alone.
