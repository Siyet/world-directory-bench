import json,time
from pathlib import Path
while True:
    t=time.time()
    vals={'time':t}
    try:
        for line in Path('/proc/1/status').read_text().splitlines():
            k,_,v=line.partition(':')
            if k in {'VmRSS','VmHWM'}:vals[k]=int(v.split()[0])
        print(json.dumps(vals),flush=True)
    except FileNotFoundError:break
    time.sleep(.2)
