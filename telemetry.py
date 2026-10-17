import json, os, sys, time
from pathlib import Path

targets = json.loads(sys.argv[1])
def counters(path):
    # Skip valueless lines such as an empty 'Groups:' of a process without supplementary groups.
    return dict(parts for line in path.read_text().splitlines() if len(parts:=line.split(maxsplit=1))==2)
def pressure(path):
    return {line.split()[0]: dict(x.split('=') for x in line.split()[1:]) for line in path.read_text().splitlines()}

while True:
    t = time.monotonic()
    out = {'unix': time.time(), 'monotonic': t, 'targets': {}, 'vm': {}}
    for name, pid in targets.items():
        try:
            proc = Path(f'/proc/{pid}')
            cg = proc.joinpath('cgroup').read_text().strip().split('::', 1)[1]
            root = Path('/sys/fs/cgroup') / cg.lstrip('/')
            status = counters(proc/'status')
            out['targets'][name] = {
                'pid': pid, 'cgroup': cg,
                'container_init_rss_kib': int(status.get('VmRSS:', '0 kB').split()[0]),
                'cpu': {k: int(v) for k,v in counters(root/'cpu.stat').items()},
                'memory_bytes': int((root/'memory.current').read_text()),
                'cpu_pressure': pressure(root/'cpu.pressure'),
                'memory_pressure': pressure(root/'memory.pressure'),
            }
        except (OSError, ValueError, IndexError) as e:
            out['targets'][name] = {'error': str(e)}
    out['vm']['cpu_stat'] = Path('/proc/stat').read_text()
    out['vm']['loadavg'] = Path('/proc/loadavg').read_text().strip()
    out['vm']['cpu_pressure'] = pressure(Path('/proc/pressure/cpu'))
    out['vm']['memory_pressure'] = pressure(Path('/proc/pressure/memory'))
    out['sample_seconds'] = time.monotonic() - t
    print(json.dumps(out), flush=True)
    time.sleep(max(0, 1-(time.monotonic()-t)))
