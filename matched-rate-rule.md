# Equal-load rate rule, series 8 (written 2026-11-18 before any point on the server)

Stage `smoke` of run.py runs one throughput point per measured variant (10 s warmup, 20 s window, labels smoke-<variant>) and two static points smoke-static-0/1 (15/30 s). Run in user.slice with AllowedCPUs=4-7 (isolate.sh).

All measured variants serve GET pages (run.py ROUTE_GROUPS has one group). MATCHED_RATES['get'] = 0.7 x the minimum smoke RPS among the measured variants, rounded down to a multiple of 100. The value is written into run.py before stage `protocol`, which refuses to run without it, and is recorded in protocol.json. The registered static_pre-0/1 points run only after the protocol is written.
