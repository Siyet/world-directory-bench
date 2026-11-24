# Equal-load rate rule, series 10 (written 2026-11-24 before any point on the server; same rule as series 9)

Stage `smoke` of run.py runs one throughput point per measured variant (10 s warmup, 20 s window, labels smoke-<variant>) and two static points smoke-static-0/1 (15/30 s). Run in user.slice with AllowedCPUs=4-5 (isolate.sh).

The measured variants form three route groups (run.py ROUTE_GROUPS): get, post (POST /cities/search) and ingest (POST /cities/validate). For each group, MATCHED_RATES[group] = 0.7 x the minimum smoke RPS among the measured variants of that group, rounded down to a multiple of 100. The values are written into run.py before stage `protocol`, which stops with an error while any of them is unset, and are recorded in protocol.json. The registered static_pre-0/1 points run only after the protocol is written.
