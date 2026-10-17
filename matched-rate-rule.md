# Equal-load rate rule (written 2026-10-13 before any msgcache point on the server; revised for the new design before any point)

Series 3 uses the same rule (stage `smoke` of run.py runs every variant plus two static points). Original text: After msgcache setup and before its protocol, one throughput smoke point per variant used at equal load (cache, cache-msgpack, pydantic-cache, fastapi-msgspec, litestar, go), 10 s warmup and 20 s window, labels smoke-<variant>, plus two pilot static points smoke-static-0/1 (15/30 s) and one smoke point of the standard pydantic variant. Command: `python3 -B -c "import run;run.point('smoke-<variant>','<variant>',2050,10,20)"` (static: `run.point('smoke-static-<i>','static',2050,15,30)`), run as a systemd unit in user.slice with AllowedCPUs=4-5.

MATCHED_RATE = 0.7 x the minimum smoke RPS among the six equal-load variants, rounded down to a multiple of 100. The registered static_pre-0/1 points run only after the protocol is written.
