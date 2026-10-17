"""Aggregate Linux host counters only; no process or command-line inventory."""
import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

FIELDS = ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--interval', type=float, default=10.0)
    args = parser.parse_args()
    if not sys.platform.startswith('linux'):
        raise RuntimeError('Linux /proc/stat is required')
    if not 0 < args.interval < float('inf'):
        raise ValueError('interval must be positive and finite')
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    deadline = time.monotonic()
    while True:
        stat = Path('/proc/stat').read_text().splitlines()
        total = stat[0].split()
        if total[0] != 'cpu':
            raise RuntimeError('unexpected /proc/stat layout')
        ticks = dict(zip(FIELDS, map(int, total[1:1 + len(FIELDS)])))
        per_cpu = {line.split()[0]: dict(zip(FIELDS, map(int, line.split()[1:1 + len(FIELDS)])))
                   for line in stat[1:] if line.startswith('cpu')}
        print(json.dumps({'unix': time.time(), 'monotonic': time.monotonic(),
                          'cpu_ticks': ticks, 'per_cpu_ticks': per_cpu,
                          'clock_ticks_per_second': os.sysconf('SC_CLK_TCK'),
                          'logical_cpus': os.cpu_count(),
                          'loadavg_1_5_15': os.getloadavg(),
                          'cpu_pressure': Path('/proc/pressure/cpu').read_text(),
                          'scope': 'aggregate KVM guest; steal is time taken by the hypervisor'}),
              flush=True)
        deadline += args.interval
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            deadline = time.monotonic()
        else:
            time.sleep(remaining)


if __name__ == '__main__':
    main()
