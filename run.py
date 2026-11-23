import hashlib, json, os, random, subprocess, sys, time, urllib.parse, urllib.request
from pathlib import Path

R = Path(__file__).resolve().parent
F = R/'bench'
E = R/'evidence'
API='msgspec-talk-s8-api'; GEN='msgspec-talk-s8-gen'
PG='msgspec-talk-s8-pg'; REDIS='msgspec-talk-s8-redis'
NET='msgspec-talk-s8'; SAMPLE='msgspec-talk-s8-sample'; PORT=18280
# As in series 7: 20 closed-loop clients; GET traffic mixes one hot page with a unique cold page on
# every tenth request; the view cache is disabled for every variant.
WORKERS=20; COLD_EVERY=10
COLD_POOL=80000; COLD_OFFSETS={'warmup':0,'measurement':25000}
# The API image is built from bench/service/Dockerfile-T (CPython 3.14.10, requirements-T.lock) on the
# server; its image ID is written to evidence/image-T.json by the build stage and read here. The other
# images are pulled by digest (Docker Engine with the containerd image store reports these IDs).
def image_t():
    return json.loads((E/'image-T.json').read_text())['id']
DEBIAN='sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251'
POSTGRES='sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280'
REDIS_IMAGE='sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499'

def cmd(*args, check=True):
    p=subprocess.run(args, text=True, capture_output=True)
    if check and p.returncode: raise RuntimeError(f'{args}\n{p.stderr}')
    return p.stdout

def save(path, data):
    path.write_text(json.dumps(data, indent=2))

def setup():
    names=cmd('docker','ps','-a','--format','{{.Names}}').splitlines()
    if any(n in names for n in (API,GEN,PG,REDIS,SAMPLE)):
        raise RuntimeError('Own resource names already exist; inspect before reuse')
    save(E/'docker-context.json',json.loads(cmd('docker','context','inspect')))
    save(E/'docker-info.json',json.loads(cmd('docker','info','--format','{{json .}}')))
    save(E/'host-environment.json',{'uname':cmd('uname','-a'),'lscpu':cmd('lscpu'),'os_release':Path('/etc/os-release').read_text(),'meminfo_total':Path('/proc/meminfo').read_text().splitlines()[0],'python':sys.version,'allowed_cpus':{u:cmd('systemctl','show','-p','AllowedCPUs','-p','EffectiveCPUs',u) for u in ('docker.service','containerd.service','user.slice','system.slice')}})
    save(E/'existing-containers.json',names)
    cmd('docker','network','create',NET)
    cmd('docker','run','-d','--name',PG,'--network',NET,'--network-alias','postgres','--cpus','.75','--cpuset-cpus','1','--memory','1200m','--tmpfs','/var/lib/postgresql:size=900m','-e','POSTGRES_USER=directory','-e','POSTGRES_PASSWORD=directory-local','-e','POSTGRES_DB=directory',POSTGRES,'postgres','-c','max_connections=40','-c','shared_buffers=64MB','-c','max_wal_size=256MB')
    # Redis as in series 3 (no eviction, a full Redis fails loudly); series 8 stores no pages in it.
    cmd('docker','run','-d','--name',REDIS,'--network',NET,'--network-alias','redis','--cpus','.2','--cpuset-cpus','1','--memory','1024m',REDIS_IMAGE,'redis-server','--save','','--appendonly','no','--maxmemory','900mb','--maxmemory-policy','noeviction')
    for _ in range(100):
        p=subprocess.run(['docker','exec',PG,'pg_isready','-h','127.0.0.1','-U','directory','-d','directory'],capture_output=True)
        if p.returncode==0:break
        time.sleep(.2)
    else: raise RuntimeError('Database readiness failed')
    # Normalized data from data/prepare.py; its checksums are recorded in evidence/directory.load.json.
    data=Path(os.environ.get('WDB_DATA',str(R/'data'/'normalized')))
    for module,args in [('directory.init_db',[]),('directory.load',['/data']),('directory.db_size',[])]:
        result=cmd('docker','run','--rm','--name','msgspec-talk-s8-seed','--network',NET,'--cpus','1','--memory','512m','-v',str(F)+':/work:ro','-v',str(data)+':/data:ro','-e','PYTHONPATH=/work/service','-e','PYTHONDONTWRITEBYTECODE=1','-e','DATABASE_URL=postgresql+asyncpg://directory:directory-local@postgres/directory','-w','/work/service','--entrypoint','python',image_t(),'-m',module,*args)
        (E/(module+'.json')).write_text(result)
    cmd('docker','run','-d','--name',GEN,'--network',NET,'--cpus','2','--cpuset-cpus','2,3','--memory','512m','-v',str(F)+':/work:ro','-v',str(R)+':/control:ro','--entrypoint','sleep',DEBIAN,'infinity')
    print('setup complete',flush=True)

def request(path):
    with urllib.request.urlopen(f'http://127.0.0.1:{PORT}'+path,timeout=8) as r:
        return r.status,json.loads(r.read())

# Series 8: one configuration (fastapi-msgspec: MsgspecRouter without output schema, asyncpg, uvloop,
# msgspec-fastapi main through bench/router-main-shim) served by uvicorn with h11 or httptools and
# with the access log off or on. pydantic (FastAPI with Pydantic response_model, SQLAlchemy ORM, asyncio)
# and go are not measured; they record the cold pool with two independent implementations and are
# compared in preflight.
OPT=dict(path='msgspec',output='none',db='asyncpg',loop='uvloop')
VARIANTS={
'fastapi-msgspec':dict(OPT,app='diagnostic_app:app',codec='json',http='h11',access_log=False),
'fastapi-msgspec-httptools':dict(OPT,app='diagnostic_app:app',codec='json',http='httptools',access_log=False),
'fastapi-msgspec-accesslog':dict(OPT,app='diagnostic_app:app',codec='json',http='h11',access_log=True),
'pydantic':dict(path='explicit',app='diagnostic_app:app',codec='json',http='h11',access_log=False),
'go':dict(),
}
SHIM='/work/router-main-shim:/work/router-main-site:'
# uvicorn puts --app-dir first on sys.path; the shim makes the service import msgspec-fastapi main.
APP_DIR='/work/router-main-shim'
# Router of the running server: msgspec_fastapi.router imports its C extension at import time.
ROUTER_SO='/work/router-main-site/msgspec_fastapi/_defaults.abi3.so'
def check_live_router(variant):
    maps=cmd('docker','exec',API,'cat','/proc/1/maps')
    assert ROUTER_SO in maps,variant
    # httptools is imported only by the httptools protocol (uvicorn --http httptools).
    assert ('/httptools/' in maps)==(VARIANTS[variant]['http']=='httptools'),variant
PYTHONPATH=SHIM+'/work/service:/work/apps'
def uvicorn_args(x):
    return ['--host','0.0.0.0','--port','8000','--workers','1','--loop',x.get('loop','asyncio'),'--http',x['http']]+([] if x['access_log'] else ['--no-access-log'])+['--app-dir',APP_DIR]
def image_of(variant):
    return DEBIAN if variant in GO_LIKE else image_t()
POST_VARIANTS={v for v,x in VARIANTS.items() if x.get('cases') in ('post','ingest')}
GO_LIKE={'go','static'}
CACHED=set()
GC_PROBE={v for v,x in VARIANTS.items() if v not in GO_LIKE and not x.get('cases')}
SERIALIZER={'json':{'check_repr':True,'pickler':'JsonPickler'},'msgpack':{'check_repr':False,'pickler':'NonPickler'}}
STORED_PREFIX={}
REF='pydantic'

def start(variant, seed):
    cmd('docker','rm','-f',API,check=False)
    if variant in CACHED:cmd('docker','exec',REDIS,'redis-cli','FLUSHDB')
    args=['docker','run','-d','--name',API,'--network',NET,'--network-alias','api','--cpus','1','--cpuset-cpus','0','--memory','512m','-p',f'127.0.0.1:{PORT}:8000','-v',str(F)+':/work:ro']
    if variant=='go':
        args+=['-e','GOMAXPROCS=1','-e','DATABASE_URL=postgres://directory:directory-local@postgres/directory','-e','VIEW_CACHE=disabled','--entrypoint','/work/bin/go-server-full',DEBIAN]
    elif variant=='static':
        args+=['-e','GOMAXPROCS=1','--entrypoint','/work/bin/static',DEBIAN,'-body','/work/body.json','-sha256',hashlib.sha256((F/'body.json').read_bytes()).hexdigest()]
    else:
        x=VARIANTS[variant]
        args+=['-e','DATABASE_URL=postgresql+asyncpg://directory:directory-local@postgres/directory','-e','REDIS_URL=redis://redis:6379/0','-e','CACHE_MODE=bypass','-e','VIEW_CACHE='+x.get('viewcache','disabled'),'-e','CACHE_CODEC='+x['codec'],'-e','RESPONSE_PATH='+x['path'],'-e','OUTPUT_MODEL='+x.get('output','schema'),'-e','DB_BACKEND='+x.get('db','orm'),'-e','PYTHONHASHSEED='+str(seed),'-e','PYTHONPATH='+PYTHONPATH,'-e','BENCH_VARIANT='+variant,'-e','PYTHONDONTWRITEBYTECODE=1','-w','/work/service','--entrypoint','uvicorn',image_of(variant),x['app'],*uvicorn_args(x)]
    cmd(*args)
    for _ in range(100):
        try:
            status,body=request('/cities?limit=100')
            if status==200:
                assert body==json.loads((F/'cases.json').read_text())[0]['expected']
                return
        except (OSError,ValueError):pass
        time.sleep(.2)
    logs=subprocess.run(['docker','logs',API],text=True,capture_output=True)
    raise RuntimeError('API readiness failed: '+logs.stdout+logs.stderr)

def traffic(variant, phase):
    """Generator arguments for the request mix of a variant; phase selects the cold offset."""
    if variant in POST_VARIANTS:return ['-cases','/work/'+VARIANTS[variant]['cases']+'-cases.json']
    if variant=='static':return ['-cases','/work/cases.json']
    return ['-cases','/work/cases.json','-cold-cases','/work/cold-cases.json','-cold-every',str(COLD_EVERY),'-cold-offset',str(COLD_OFFSETS[phase])]

def expected_cache(variant):
    # /cities/validate has no cache semantics and sends no X-Cache header (checked in preflight).
    if variant in CACHED:return 'hit-or-miss'
    return '' if VARIANTS.get(variant,{}).get('cases')=='ingest' else 'bypass'

def load(duration, out, procs=2, cache="bypass", variant='static', phase='measurement'):
    args=['docker','exec',GEN,'/control/bin/http-load','-mode','throughput','-url','http://api:8000',*traffic(variant,phase),'-workers',str(WORKERS),'-duration',str(duration),'-warmup','2','-expected-cache',cache,'-gomaxprocs',str(procs)]
    p=subprocess.run(args,text=True,capture_output=True)
    save(out, {'args':args,'exit_code':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
    if p.returncode:raise RuntimeError(p.stdout+p.stderr)
    result=json.loads(p.stdout)
    assert all(result[k]['errors']==0 for k in ('warmup','in_window','drain'))
    return result

def load_open(duration, out, rate, procs=2, variant='static'):
    """Open-loop measurement at a fixed offered rate, for API CPU per response at equal load."""
    args=['docker','exec',GEN,'/control/bin/http-load','-mode','open-loop','-url','http://api:8000',*traffic(variant,'measurement'),'-rps',str(rate),'-duration',str(duration),'-warmup','2','-gomaxprocs',str(procs)]
    p=subprocess.run(args,text=True,capture_output=True)
    save(out, {'args':args,'exit_code':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
    if p.returncode:raise RuntimeError(p.stdout+p.stderr)
    result=json.loads(p.stdout)
    assert result['error_fraction_of_offered']==0 and result['generator_schedule_ok'],result
    return result

def point(label,variant,seed,warmup,seconds,procs=2,rate=None):
    out=E/label
    out.mkdir(exist_ok=False)
    observed=set(cmd('docker','ps','-a','--format','{{.Names}}').splitlines())
    assert observed <= {API,GEN,PG,REDIS}, ('unexpected containers',observed)
    assert cmd('docker','inspect',REDIS,'--format','{{.Image}}').strip()==REDIS_IMAGE
    start(variant,seed)
    info=json.loads(cmd('docker','inspect',API,GEN,PG,REDIS))
    save(out/'inspect.json',info)
    assert info[0]['Image']==image_of(variant)
    assert info[1]['Image']==DEBIAN and info[2]['Image']==POSTGRES and info[3]['Image']==REDIS_IMAGE
    expected={API:(1000000000,'0',512*1024**2),GEN:(procs*1000000000,'2,3',512*1024**2),PG:(750000000,'1',1200*1024**2),REDIS:(200000000,'1',1024*1024**2)}
    for obj in info:
        name=obj['Name'].lstrip('/')
        hc=obj['HostConfig']
        assert (hc['NanoCpus'],hc['CpusetCpus'],hc['Memory'])==expected[name],name
        assert hc['LogConfig']=={'Type':'json-file','Config':{}},(name,hc['LogConfig'])
    if variant not in GO_LIKE:
        env=dict(x.split('=',1) for x in info[0]['Config']['Env'])
        x=VARIANTS[variant]
        for key,value in {'PYTHONPATH':PYTHONPATH,'BENCH_VARIANT':variant,'RESPONSE_PATH':x['path'],'OUTPUT_MODEL':x.get('output','schema'),'DB_BACKEND':x.get('db','orm'),'CACHE_MODE':'bypass','VIEW_CACHE':x.get('viewcache','disabled'),'CACHE_CODEC':x['codec'],'PYTHONHASHSEED':str(seed)}.items():
            assert env[key]==value,(key,env.get(key))
        c=info[0]['Config']['Cmd']
        assert info[0]['Config']['Entrypoint']==['uvicorn'] and c==[x['app'],*uvicorn_args(x)],c
        check_live_router(variant)
    elif variant=='go':
        env=dict(x.split('=',1) for x in info[0]['Config']['Env'])
        assert info[0]['Config']['Entrypoint']==['/work/bin/go-server-full'],info[0]['Config']['Entrypoint']
        assert (env.get('GOMAXPROCS'),env.get('VIEW_CACHE'),env.get('REDIS_ADDR'))==('1','disabled',None),env
    targets={x['Name'].rsplit('-',1)[-1]:x['State']['Pid'] for x in info}
    # Log pipeline outside the API cgroup: the API container's containerd shim (holds its stdout pipe)
    # and dockerd (json-file logging driver); sampled per process on the system CPUs.
    shim=int(Path(f"/proc/{targets['api']}/stat").read_text().rsplit(')',1)[1].split()[1])
    assert Path(f'/proc/{shim}/comm').read_text().strip().startswith('containerd-shim'),shim
    log_procs={'api_shim':shim,'dockerd':int(cmd('pidof','dockerd').split()[0])}
    save(out/'log-pipeline-procs.json',log_procs)
    cmd('docker','run','-d','--name',SAMPLE,'--pid','host','--cgroupns','host','--cpus','.05','--cpuset-cpus','1','--memory','64m','-v',str(R)+':/control:ro','--entrypoint','python',image_t(),'/control/telemetry.py',json.dumps(targets),json.dumps(log_procs))
    try:
        w=load(warmup,out/'warmup-attempt.json',procs,cache=expected_cache(variant),variant=variant,phase='warmup')
        if variant in GC_PROBE:save(out/'gc-before.json',request('/__bench_gc')[1])
        if rate is None:
            result=load(seconds,out/'measurement-attempt.json',procs,cache=expected_cache(variant),variant=variant)
        else:
            result=load_open(seconds,out/'measurement-attempt.json',rate,procs,variant=variant)
            result['goodput_rps']=result['goodput_in_window_rps']
        if variant in GC_PROBE:save(out/'gc-after.json',request('/__bench_gc')[1])
    finally:
        cmd('docker','stop','-t','1',SAMPLE,check=False)
        (out/'telemetry.jsonl').write_text(cmd('docker','logs',SAMPLE,check=False))
        cmd('docker','rm',SAMPLE,check=False)
    if variant in CACHED:
        # Storage format of the serving process at every point (refill once if the
        # 60 s entry has just expired).
        # Thousands of cold entries expiring every second: sample atomically inside Redis
        # (one script: SCAN, skip keys with less than 3 s TTL, read the prefix).
        sampled=redis('EVAL',PREFIX_SAMPLE,'0',str(len(STORED_PREFIX[variant]))).splitlines()
        if not sampled:
            request('/cities?limit=100');sampled=redis('EVAL',PREFIX_SAMPLE,'0',str(len(STORED_PREFIX[variant]))).splitlines()
        prefixes=sorted(set(sampled))
        assert sampled and prefixes==[STORED_PREFIX[variant]],(variant,prefixes)
    save(out/'result.json',{'variant':variant,'seed':seed,'warmup':w,'result':result})
    samples=[json.loads(line) for line in (out/'telemetry.jsonl').read_text().splitlines()]
    measured=[s for s in samples if result['measurement_start_unix'] <= s['unix'] < result['measurement_end_unix']]
    assert len(measured)>=int(seconds*.9), ('missing telemetry',len(measured))
    for s in samples:
        assert set(s['targets'])==set(targets)
        assert all('error' not in t for t in s['targets'].values()),s
        assert set(s['procs'])==set(log_procs) and all('error' not in t for t in s['procs'].values()),s
    for a,b in zip(samples,samples[1:]):
        assert b['monotonic']>a['monotonic']
        for target in targets:
            assert b['targets'][target]['cpu']['usage_usec']>=a['targets'][target]['cpu']['usage_usec']
        for name in log_procs:
            assert b['procs'][name]['utime_usec']+b['procs'][name]['stime_usec']>=a['procs'][name]['utime_usec']+a['procs'][name]['stime_usec']
    if variant in CACHED:
        c=result['in_window']['cache'] if rate is None else result['cache'];hits,misses=c.get('hit',0),c.get('miss',0)
        assert set(c)<={'hit','miss'} and 0.88<=hits/(hits+misses)<=0.905,('cache hit share outside the 90% mix',c)
        kinds=result['in_window']['cache_by_kind'] if rate is None else result['cache_by_kind_in_window']
        # Every cold page is a miss; the hot page misses only when its 60 s entry expires
        # (Cashews protected=True coalesces concurrent misses of one key; the bound allows up to
        # WORKERS misses per expiry). Requests waiting on a coalesced miss are labelled hit.
        assert set(kinds.get('cold',{}))<={'miss'} and kinds['hot'].get('miss',0)<=3*WORKERS,('cache mix',kinds)
    save(out/'telemetry-check.json',{'samples_in_window':len(measured),'pass':True,'process_rss_scope':'container init process only, API valid; generator sleep and PostgreSQL parent exclude children'})
    print(json.dumps({'point':label,'variant':variant,'rps':result['goodput_rps']}),flush=True)

def run_main_points():
    protocol=json.loads((E/'protocol.json').read_text())
    bindings=json.loads((E/'source-bindings.json').read_text())
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in bindings.items())
    assert not list(F.rglob('__pycache__')),'bytecode in the measured tree'
    assert all((E/f'static-pre-{i}'/'result.json').exists() for i in range(2)),'static controls before main are required'
    for item in protocol['schedule']:
        pair=protocol['pairs'][item['pair']]
        for pos,side in enumerate(item['order']):
            variant=pair[side]
            label=f"main-{item['round']:02}-{item['pair']}-{pos}-{side}"
            if (E/label).exists():raise RuntimeError('Existing point; inspect rather than silently resume: '+label)
            point(label,variant,2100+item['round'],protocol['warmup_seconds'],protocol['measurement_seconds'])
            check_monitors()
            assert not list(F.rglob('__pycache__')),'bytecode in the measured tree'
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in bindings.items())



TIMERS=('apt-daily.timer','apt-daily-upgrade.timer','dpkg-db-backup.timer','man-db.timer','e2scrub_all.timer','fstrim.timer','systemd-tmpfiles-clean.timer','sysstat-collect.timer','sysstat-summary.timer')
SYSTEM_CPUS={4,5,6,7}
def nic_counts_on_bench_cpus(interrupts):
    lines=interrupts.splitlines();cpus=lines[0].split()
    return {l.split(':')[0].strip():sum(int(x) for c,x in zip(cpus,l.split(':',1)[1].split()) if int(c[3:])<4) for l in lines[1:] if 'virtio0-' in l}
def cpu_list(text):
    out=set()
    for part in filter(None,text.split(',')):
        a,_,b=part.partition('-');out|=set(range(int(a),int(b or a)+1))
    return out
def host_guard(tag):
    """Snapshot processes, interrupts and units, then assert the runtime isolation of CPUs 0-3 (NUMA node 0)."""
    assert [Path(f'/sys/devices/system/node/node{n}/cpulist').read_text().strip() for n in (0,1)]==['0-3','4-7']
    assert Path('/sys/devices/virtual/workqueue/cpumask').read_text().strip()=='f0'
    irqs={};irq_masks={}
    for d in Path('/proc/irq').iterdir():
        if d.is_dir() and (d/'effective_affinity_list').exists():irqs[d.name]=(d/'effective_affinity_list').read_text().strip()
        if d.is_dir() and (d/'smp_affinity_list').exists():irq_masks[d.name]=(d/'smp_affinity_list').read_text().strip()
    interrupts=Path('/proc/interrupts').read_text()
    nic=[line.split(':')[0].strip() for line in interrupts.splitlines() if 'virtio0-' in line]
    units={u:cmd('systemctl','show','-p','AllowedCPUs',u,check=False).strip() for u in ('docker.service','containerd.service','user.slice','init.scope')}
    timers={t:cmd('systemctl','is-active',t,check=False).strip() for t in TIMERS}
    procs=[]
    for d in Path('/proc').iterdir():
        if not d.name.isdigit():continue
        try:
            status=dict(x.split(':',1) for x in (d/'status').read_text().splitlines() if ':' in x)
            cg=(d/'cgroup').read_text().strip()
            started=int((d/'stat').read_text().rsplit(')',1)[1].split()[19])
        except (OSError,IndexError,ValueError):continue
        if status.get('PPid','').strip()=='2' or d.name=='2':continue
        procs.append({'pid':int(d.name),'comm':status.get('Name','').strip(),'cpus':status.get('Cpus_allowed_list','').strip(),'cgroup':cg,'start_ticks_since_boot':started})
    loose=[x for x in procs if 'docker-' not in x['cgroup'] and x['cpus']!='4-7']
    snapshot={'tag':tag,'unix':time.time(),'uptime':Path('/proc/uptime').read_text().split()[0],'affinity':sorted(os.sched_getaffinity(0)),'units':units,'timers':timers,'irq_effective_affinity':irqs,'irq_smp_affinity':irq_masks,'interrupts':interrupts,'processes':procs,'outside_system_cpus_non_container':loose}
    save(E/f'host-guard-{tag}.json',snapshot)
    failures=[]
    if not os.sched_getaffinity(0)<=SYSTEM_CPUS:failures.append('runner not on CPUs 4-7')
    failures+=[f'{u} {v}' for u,v in units.items() if v!='AllowedCPUs=4-7']
    failures+=[f'{t} {v}' for t,v in timers.items() if v!='inactive']
    # A queue without traffic keeps its old effective CPU until its next interrupt: the mask is asserted
    # at every tag, the effective CPU at the start tags, and the interrupts actually handled on CPUs 0-3
    # are compared at the end tags.
    if not nic or any(not cpu_list(irq_masks.get(n,'')) or not cpu_list(irq_masks.get(n,''))<=SYSTEM_CPUS for n in nic):failures.append(f'NIC interrupt masks {[(n,irq_masks.get(n)) for n in nic]}')
    if tag.endswith('-start') and any(not cpu_list(irqs.get(n,'')) or not cpu_list(irqs.get(n,''))<=SYSTEM_CPUS for n in nic):failures.append(f'NIC effective affinity {[(n,irqs.get(n)) for n in nic]}')
    if tag.endswith('-end'):
        before=nic_counts_on_bench_cpus(json.loads((E/f"host-guard-{tag[:-4]}-start.json").read_text())['interrupts'])
        after=nic_counts_on_bench_cpus(interrupts)
        moved={n:after[n]-before.get(n,0) for n in after if after[n]!=before.get(n,0)}
        if moved:failures.append(f'NIC interrupts handled on CPUs 0-3 during {tag[:-4]}: {moved}')
    if loose:failures.append(f'non-container processes allowed on benchmark CPUs: {loose}')
    save(E/f'host-guard-{tag}-result.json',{'pass':not failures,'failures':failures})
    assert not failures,failures

MONITORS=[]
def check_monitors():
    assert all(p.poll() is None for p in MONITORS), 'Telemetry monitor exited'
    observed=set(cmd('docker','ps','-a','--format','{{.Names}}').splitlines())
    assert observed <= {API,GEN,PG,REDIS}, ('unexpected containers',observed)
    for line in (E/'container-events.jsonl').read_text().splitlines():
        event=json.loads(line)
        assert event['name'] in {API,GEN,PG,REDIS,SAMPLE}, ('foreign container event',event)

def main():
    with (E/'host-telemetry.jsonl').open('x') as host, (E/'host-telemetry.stderr').open('x') as herr, (E/'container-events.jsonl').open('x') as events, (E/'container-events.stderr').open('x') as eerr:
        MONITORS.append(subprocess.Popen([sys.executable,str(R/'host_telemetry.py')],stdout=host,stderr=herr))
        args=['docker','events','--filter','type=container']
        for action in ('create','start','die','destroy','rename'):
            args += ['--filter','event='+action]
        args += ['--format','{"time":{{.TimeNano}},"action":{{json .Action}},"id":{{json .Actor.ID}},"name":{{json .Actor.Attributes.name}}}']
        MONITORS.append(subprocess.Popen(args,stdout=events,stderr=eerr))
        try:
            time.sleep(1)
            host_guard('main-start')
            check_monitors()
            run_main_points()
            check_monitors()
            save(E/'main-complete.json', {'points':len(json.loads((E/'protocol.json').read_text())['schedule'])*4,'source_bindings_verified':True,'monitor_guards_passed':True})
            host_guard('main-end')
        finally:
            for proc in MONITORS:
                if proc.poll() is None:proc.terminate()
            for proc in MONITORS:proc.wait(timeout=10)

def redis(*args):
    return cmd('docker','exec',REDIS,'redis-cli',*args)

def cache_checks(variant):
    """Black-box cache contract on the running API: miss then hit, hit without
    PostgreSQL, rejection of a corrupted cached entry, TTL bound."""
    import urllib.error
    redis('FLUSHDB')
    path='/cities?limit=17'
    def get():
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{PORT}'+path,timeout=8) as r:
                return r.status,r.headers.get_all('X-Cache'),r.read()
        except urllib.error.HTTPError as e:
            return e.code,e.headers.get_all('X-Cache'),e.read()
    first=get(); assert first[0]==200 and first[1]==['miss'],first[:2]
    keys=redis('--scan').split()
    assert len(keys)==1,keys
    ttl=int(redis('TTL',keys[0])); assert 0<ttl<=60,ttl
    second=get(); assert second[0]==200 and second[1]==['hit'] and json.loads(second[2])==json.loads(first[2]),second[:2]
    cmd('docker','pause',PG)
    try:
        nodb=get()
    finally:
        cmd('docker','unpause',PG)
    assert nodb[0]==200 and nodb[1]==['hit'] and json.loads(nodb[2])==json.loads(first[2]),nodb[:2]
    # Black-box proof of the storage format used by the serving process.
    prefix=redis('GETRANGE',keys[0],'0',str(len(STORED_PREFIX[variant])-1)).rstrip('\n')
    assert prefix==STORED_PREFIX[variant],(variant,prefix)
    if variant in MSGPACK:
        # Binary entry: corrupt and re-shape it inside the API container (msgspec, redis-py).
        tamper=lambda mode:cmd('docker','exec','-w','/work/service',API,'python','-c',TAMPER,keys[0],mode).strip()
        # Valid entry with keys in another order: decoded, validated and re-encoded (200).
        assert tamper('reorder')=='ok'
        nc=get()
        assert tamper('corrupt')=='ok'
        bad=get(); assert bad[0]>=500,bad
    else:
        raw=redis('GET',keys[0]).rstrip('\n')
        import re
        # Keep the original spacing so the value check, not the encoding, rejects it.
        corrupted,n=re.subn(r'("population": ?)[0-9]+',r'\1-1',raw,count=1)
        assert n==1,raw[:200]
        assert redis('SET',keys[0],corrupted,'KEEPTTL').strip()=='OK'
        bad=get(); assert bad[0]>=500,bad
        # Valid but non-canonical entry: one extra space.
        noncanon=raw.replace('{"items"','{ "items"',1); assert noncanon!=raw
        assert redis('SET',keys[0],noncanon,'KEEPTTL').strip()=='OK'
        nc=get()
    # Python variants send the re-encoded validated object (200); Go accepts only its
    # own canonical bytes (500).
    if variant in GO_LIKE:assert nc[0]>=500,nc[:2]
    else:assert nc[0]==200 and nc[1]==['hit'] and nc[2]==first[2],nc[:2]
    redis('FLUSHDB')
    refill=get(); assert refill[0]==200 and refill[1]==['miss'] and json.loads(refill[2])==json.loads(first[2]),refill[:2]
    redis('FLUSHDB')
    return {'stored_prefix':prefix,'first':'miss','second':'hit','hit_with_postgres_paused':'hit','corrupted_entry_status':bad[0],'noncanonical_entry_status':nc[0],'refill':'miss','ttl_seconds':ttl,'key':keys[0]}

MSGPACK={'cache-msgpack'}
PREFIX_SAMPLE="""local out={} local cur='0'
repeat
  local r=redis.call('SCAN',cur,'COUNT',500) cur=r[1]
  for _,k in ipairs(r[2]) do
    if #out<20 and redis.call('TTL',k)>2 then table.insert(out,redis.call('GETRANGE',k,0,tonumber(ARGV[1])-1)) end
  end
until cur=='0' or #out>=20
return out"""
TAMPER='''
import sys, msgspec, redis
r = redis.Redis(host='redis')
key, mode = sys.argv[1], sys.argv[2]
raw = r.get(key)
assert raw.startswith(b'bytes:')
page = msgspec.msgpack.decode(raw[6:])
if mode == 'corrupt':
    page['items'][0]['population'] = -1
else:
    page = {'next_after_id': page['next_after_id'], 'items': page['items']}
new = b'bytes:' + msgspec.msgpack.encode(page)
assert new != raw
assert r.set(key, new, keepttl=True)
print('ok')
'''

PROBE='''
import asyncio, importlib, json, sys
m = importlib.import_module(sys.argv[1])
lifespan = getattr(m, 'lifespan', None) or importlib.import_module('directory.api').lifespan
async def main():
    async with lifespan(m.app):
        backends = list(m.app.state.service.cache._backends.values())
        s = backends[0]._serializer
        print(json.dumps({'backends': len(backends), 'check_repr': s._check_repr, 'pickler': type(s._pickler).__name__, 'codec': m.app.state.cache_codec}))
asyncio.run(main())
'''

def repr_probe(variant):
    x=VARIANTS[variant]
    out=json.loads(cmd('docker','exec','-w','/work/service',API,'python','-c',PROBE,x['app'].split(':')[0]))
    assert out=={'backends':1,**SERIALIZER[x['codec']],'codec':x['codec']},(variant,out)
    return out

STACK_PROBE='''
import hashlib, importlib, importlib.metadata as md, json, platform, sys
# Same first sys.path entry as uvicorn with this --app-dir ('' is the working directory).
sys.path[0] = sys.argv[2]
dists = sys.argv[1].split(',')
out = {'python': platform.python_version(), 'dists': {}, 'modules': {}}
for d in dists:
    out['dists'][d] = md.version(d)
for name in ('fastapi', 'starlette', 'pydantic', 'msgspec', 'sqlalchemy', 'asyncpg', 'uvloop', 'fastapi_extension_msgspec', 'msgspec_fastapi'):
    try:
        m = importlib.import_module(name)
    except ImportError:
        out['modules'][name] = None
        continue
    out['modules'][name] = m.__file__
import fastapi_extension_msgspec.msgspec_router as r
out['router_module'] = r.__file__
out['router_impl'] = r.MsgspecRouter.__module__
print(json.dumps(out))
'''
PROBE_DISTS=('fastapi','starlette','pydantic','pydantic_core','msgspec','SQLAlchemy','asyncpg','uvicorn','uvloop','h11','httptools','orjson','redis','cashews')
def lock_versions(name):
    out={}
    for line in (F/'service'/name).read_text().splitlines():
        if '==' in line:
            k,v=line.split('==');out[k.split('[')[0].lower().replace('_','-')]=v
    return out
def stack_probe(variant):
    """Actual interpreter, package versions, router files and uvicorn settings inside the running API container."""
    x=VARIANTS[variant]
    out=json.loads(cmd('docker','exec','-w','/work/service',API,'python','-c',STACK_PROBE,','.join(PROBE_DISTS),APP_DIR))
    lock=lock_versions('requirements-T.lock')
    want={d:lock[d.lower().replace('_','-')] for d in PROBE_DISTS}
    got={d:out['dists'][d] for d in PROBE_DISTS}
    assert got==want,(variant,got,want)
    assert out['python']=='3.14.10',(variant,out['python'])
    m=out['modules']
    assert m['fastapi_extension_msgspec'].startswith('/work/router-main-shim/') and m['msgspec_fastapi'].startswith('/work/router-main-site/msgspec_fastapi/'),(variant,m)
    assert out['router_impl']=='msgspec_fastapi.router',(variant,out)
    check_live_router(variant)
    out['access_log_lines']=access_log_check(variant)
    return out
def access_log_check(variant):
    """The access log writes one line per response to the container's stdout only when it is on."""
    before=len(subprocess.run(['docker','logs',API],text=True,capture_output=True).stdout.splitlines())
    for _ in range(5):request('/cities?limit=3')
    time.sleep(.5)
    lines=subprocess.run(['docker','logs',API],text=True,capture_output=True).stdout.splitlines()[before:]
    hits=[l for l in lines if '"GET /cities?limit=3 HTTP/1.1" 200' in l]
    assert len(hits)==(5 if VARIANTS[variant]['access_log'] else 0),(variant,lines)
    return len(hits)

def preflight():
    import urllib.error
    paths=['/cities?limit=100','/cities?limit=1','/cities?limit=0','/cities?limit=1001','/cities?limit=x','/cities?unknown=x','/cities?country_code=RU&limit=3','/cities?min_population=1000000&limit=3','/cities?name_prefix=M&limit=3','/cities?limit=%2B1','/cities?limit=1&limit=2','/cities?country_code=long-code&limit=3','/cities?after_id=free%20text&limit=3','/cities?division_id=free%20text&limit=3']
    # A sample of cold pages is compared across all variants as well.
    cold_cases=json.loads((F/'cold-cases.json').read_text())
    paths+=['/cities?'+urllib.parse.urlencode(c['params']) for c in random.Random(7).sample(cold_cases,25)]
    post_valid=[c['body'] for c in json.loads((F/'post-cases.json').read_text())]
    post_invalid=[b'{"unknown":1}',b'{"limit":0}',b'{"limit":"10"}',b'{"order":"name"}',b'{"filter":{"country_codes":[]}}',
        b'{"filter":{"population":{"min":10,"max":1}}}',b'{"filter":{"name_prefix":"a\\u0000"}}',b'[]',b'{"limit":',b'{"filter":{"feature_codes":["XXX"]}}']
    baseline=None;post_baseline=None;ingest_baseline=None;result={}
    for variant in VARIANTS:
        start(variant,2000);values={}
        for rep in ('first','second'):
            for path in paths:
                try:status,body=request(path)
                except urllib.error.HTTPError as e:status,body=e.code,json.loads(e.read())
                if rep=='first':values[path]={'status':status,'body':body}
                else:assert values[path]=={'status':status,'body':body},(variant,path)
        if baseline is None:baseline=values
        else:assert values==baseline,(variant,values,baseline)
        posts=None
        if variant!='go':
            posts=[]
            for raw in [json.dumps(b).encode() for b in post_valid]+post_invalid:
                req=urllib.request.Request(f'http://127.0.0.1:{PORT}/cities/search',data=raw,headers={'Content-Type':'application/json'},method='POST')
                try:
                    with urllib.request.urlopen(req,timeout=8) as r:posts.append({'status':r.status,'body':json.loads(r.read())})
                except urllib.error.HTTPError as e:posts.append({'status':e.code,'body':json.loads(e.read())})
            # Copy: the ingest results are appended to posts below.
            if post_baseline is None:post_baseline=list(posts)
            else:assert posts==post_baseline,(variant,posts)
            assert [x['status'] for x in posts]==[200]*len(post_valid)+[422]*(len(post_invalid)-2)+[400,422],[x['status'] for x in posts]
            batch=json.loads((F/'ingest-cases.json').read_text())[0]['body']['items']
            bad=lambda **c:json.dumps({'items':batch[:3]+[dict(batch[3],**c)]}).encode()
            ingest_raw=[json.dumps({'items':batch}).encode(),bad(population=-1),bad(extra=1),bad(latitude='1.0'),bad(id='geonames:country:1'),json.dumps({'items':[]}).encode(),json.dumps({'items':batch[:2],'x':1}).encode(),b'{"items":[']
            for raw in ingest_raw:
                req=urllib.request.Request(f'http://127.0.0.1:{PORT}/cities/validate',data=raw,headers={'Content-Type':'application/json'},method='POST')
                try:
                    with urllib.request.urlopen(req,timeout=8) as r:posts.append({'status':r.status,'body':json.loads(r.read())})
                except urllib.error.HTTPError as e:posts.append({'status':e.code,'body':json.loads(e.read())})
            assert [x['status'] for x in posts[-len(ingest_raw):]]==[200]+[422]*6+[400],posts[-len(ingest_raw):]
            req=urllib.request.Request(f'http://127.0.0.1:{PORT}/cities/validate',data=ingest_raw[0],headers={'Content-Type':'application/json'},method='POST')
            with urllib.request.urlopen(req,timeout=8) as r:assert r.headers.get_all('X-Cache') is None,r.headers.get_all('X-Cache')
            if ingest_baseline is None:ingest_baseline=posts[-len(ingest_raw):]
            else:assert posts[-len(ingest_raw):]==ingest_baseline,(variant,posts[-len(ingest_raw):])
        if variant in CACHED:
            cache=cache_checks(variant);probe=repr_probe(variant)
        else:
            # Without a cache: header bypass and nothing written to Redis.
            redis('FLUSHDB')
            with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/cities?limit=17',timeout=8) as r:
                assert r.headers.get_all('X-Cache')==['bypass'],r.headers.get_all('X-Cache')
            assert redis('DBSIZE').strip()=='0';cache={'x_cache':'bypass','redis_keys':0};probe=None
        load(1,E/f'preflight-{variant}-load.json',cache=expected_cache(variant),variant=variant)
        result[variant]={'pass':True,'cases':values,'post_cases':posts,'cache':cache,'serializer':probe,'stack':None if variant=='go' else stack_probe(variant)}
    save(E/'preflight.json',result)
    print('preflight all variants passed',json.dumps({v:r['cache'] for v,r in result.items()}),flush=True)

def psql(sql):
    return cmd('docker','exec',PG,'psql','-U','directory','-d','directory','-At','-F','\t','-c',sql)

def cold():
    """Cold GET pool: unique full pages after random city ids. Expected canonical hashes are
    recorded from two independent implementations (Pydantic + SQLAlchemy ORM, Go + pgx) and must agree."""
    ids=psql('SELECT id FROM cities ORDER BY id').split()
    assert len(ids)>COLD_POOL+200,len(ids)
    rng=random.Random(202611170700)
    chosen=rng.sample(ids[:-101],COLD_POOL)
    pool=[{'path':'/cities','params':{'limit':'100','after_id':i}} for i in chosen]
    save(E/'cold-pool.json',pool)
    recorded={}
    for variant in (REF,'go'):
        start(variant,2000)
        p=subprocess.run(['docker','exec',GEN,'/control/bin/http-load','-mode','record','-url','http://api:8000','-cold-cases','/control/evidence/cold-pool.json','-workers','4'],text=True,capture_output=True)
        (E/f'cold-record-{variant}.stderr').write_text(p.stderr)
        assert p.returncode==0,p.stderr[-2000:]
        recorded[variant]=json.loads(p.stdout)
        save(E/f'cold-record-{variant}.json',recorded[variant])
    a,b=recorded[REF],recorded['go']
    assert len(a)==len(b)==COLD_POOL and all(x['canonical_sha256'] for x in a)
    assert [(x['path'],x['params'],x['canonical_sha256']) for x in a]==[(x['path'],x['params'],x['canonical_sha256']) for x in b],'implementations disagree on cold pages'
    assert len({x['canonical_sha256'] for x in a})==COLD_POOL,'cold pages must be distinct'
    (F/'cold-cases.json').write_text(json.dumps(a))
    cmd('docker','rm','-f',API,check=False)
    print('cold pool recorded',len(a),flush=True)

# POST search and ingest cases (bench/post-cases.json, bench/ingest-cases.json) are reused from series 7
# unchanged; preflight checks every variant against them.
PAIRS={
# uvicorn HTTP protocol: h11 (as in all earlier series) versus httptools.
'h11-httptools':{'A':'fastapi-msgspec','B':'fastapi-msgspec-httptools'},
# uvicorn access log on (the uvicorn default) versus off (--no-access-log, as in all earlier series).
'accesslog-off-on':{'A':'fastapi-msgspec','B':'fastapi-msgspec-accesslog'},
# Noise reference.
'aa-get-fast':{'A':'fastapi-msgspec','B':'fastapi-msgspec'},
}
MEASURED=['fastapi-msgspec','fastapi-msgspec-httptools','fastapi-msgspec-accesslog']
assert {v for x in PAIRS.values() for v in x.values()}==set(MEASURED)
ROUTE_GROUPS={'get':MEASURED}

def smoke():
    for variant in MEASURED:
        point(f'smoke-{variant}',variant,2050,10,20)
    for i in range(2):point(f'smoke-static-{i}','static',2050,15,30)

def make_protocol():
    assert all(isinstance(MATCHED_RATES[g],int) and MATCHED_RATES[g]>0 for g in ROUTE_GROUPS),'set MATCHED_RATES from the smoke points first'
    rng=random.Random(202611180900)
    pairs=PAIRS
    orders={name:rng.sample(['ABBA']*4+['BAAB']*4,8) for name in pairs}
    schedule=[]
    for rnd in range(8):
        names=list(pairs);rng.shuffle(names)
        schedule+=[{'round':rnd,'pair':name,'order':orders[name][rnd]} for name in names]
    sources=[q for q in sorted(F.rglob('*')) if q.is_file() and '__pycache__' not in q.parts]
    save(E/'source-bindings.json',{str(q):hashlib.sha256(q.read_bytes()).hexdigest() for q in sources}|{str(R/n):hashlib.sha256((R/n).read_bytes()).hexdigest() for n in ('run.py','telemetry.py','host_telemetry.py','analyze.py','bin/http-load','matched-rate-rule.md','evidence/cold-pool.json','evidence/image-T.json','evidence/router-main.json')}|{str(q):hashlib.sha256(q.read_bytes()).hexdigest() for q in sorted((R/'generator').glob('*')) if q.is_file()})
    rules={
    'lower_bound':'A variant whose mean API CPU over all its main points is below 0.95 core is reported as an RPS lower bound (client or latency limited), with API CPU per response as secondary metric, unless the pair is flagged by direction_under_client_limit, where it is the primary metric; never as a server capacity ratio. Classified once per variant.',
    'direction_under_client_limit':'Generator saturation is anchored to generator CPU in the static controls (static-pre before main, static after main; mean of all four). Static RPS is not used as a limit, because generator cost per response differs between the static Go server and uvicorn responses. If either side of a pair has mean generator CPU >= 0.9 x static generator CPU, the RPS direction of that pair is not reported as a server result; API CPU per response is then the primary metric. Generator CPU per response is reported per variant.',
    'pair_ratio':'The pair ratio is the measured throughput ratio with 20 closed-loop clients and memoized body verification on 2 generator CPUs; not a capacity ratio. A direction may be claimed only if the nominal interval excludes 0. The same rule applies to API CPU per response (comparisons[pair].api_cpu_us_per_response, same estimator). The primary metric of each pair is fixed in advance: RPS, or API CPU per response for pairs flagged by direction_under_client_limit; the other metric is descriptive and never replaces a null primary result. When neither side is a lower bound (API at least 0.95 core on both), the CPU interval mirrors the RPS interval and is not independent evidence. analyze.py records the decision per pair: primary_metric, primary_direction_claim_allowed, rps_direction_claim_allowed, cpu_compared_at_different_loads and cpu_interval_mirrors_rps.',
    'redis':'Redis gets 0.2 CPU and 1024 MiB on cpuset 1 (maxmemory 900mb, noeviction) as in series 3 and 7; series 8 has no view-cache variants, so Redis holds no pages. If Redis throttled time exceeds 1% of the measurement window in any point of a variant, that variant is reported as Redis-limited.',
    'host_drift':'The host is a cloud KVM VM with 8 vCPUs in two NUMA nodes (0-3 and 4-7); exclusive physical cores are not confirmed by the provider documentation, and zero steal does not exclude cache or memory-bandwidth contention. Per-point steal and softirq shares of CPUs 0-3 are reported and a point with steal above 1% of the window is flagged. Static controls before and after main are compared by RPS, generator CPU per response and API CPU per response; a difference above 10% in RPS or generator CPU per response flags the series as host-drifted, reported with the results. The per-round values of fastapi-msgspec (both sides of aa-get-fast and one side of the other two pairs, every round) are reported as an in-series drift trace. Per-response CPU values of this series are never set next to values of other series.',
    'equal_load':'After main and headroom, every measured variant runs two open-loop points at %s offered RPS (GET), 30 s after a 15 s throughput warmup, order as listed then reversed. The rate follows matched-rate-rule.md: 0.7 x the minimum throughput smoke RPS of the measured variants on this server, rounded down to a multiple of 100, fixed before this protocol. API CPU per response at equal load is unpaired and reported descriptively (no interval) alongside the paired main-series value, which depends on load.'%MATCHED_RATES['get'],
    'request_mix':'Every tenth request (global counter) is a unique cold page from the pool, taken sequentially from offset 0 in the warmup run and from offset 25000 in the measurement run (pool 80000) of each point; an exhausted pool is a client error. Every variant serves this mix from PostgreSQL (CACHE_MODE=bypass, VIEW_CACHE=disabled). Static controls use only the hot page.',
    'headroom':'After main, the client 1/2/2/1 CPU sweep runs for fastapi-msgspec, plus two static controls.',
    'path_disclosure':'All three measured variants are the same application and process configuration: MsgspecRouter without output schema (msgspec-fastapi main through bench/router-main-shim), direct asyncpg queries, uvloop, one uvicorn worker. h11-httptools changes only --http (h11 to httptools); accesslog-off-on changes only the access log (--no-access-log on side A, the uvicorn default access log on side B, so B/A is the cost of the log). The access log is written by the uvicorn.access logger to the container stdout, collected by the Docker json-file logging driver. That collection runs in the containerd shim of the API container and in dockerd on the system CPUs, outside the API cgroup: it is not included in API CPU per response and is reported separately per point (log_pipeline in analyze.py, the per-process CPU of the shim and of dockerd; dockerd also serves the runner and the sampler, so only the difference between the sides of accesslog-off-on is attributable to the access log). Unbound kernel work queues are restricted to CPUs 4-7. Python logging writes to stdout synchronously: if the pipe were ever full, the event loop would wait and RPS would fall without API CPU rising, and the lower_bound rule then reports that variant as an RPS lower bound (mean API CPU below 0.95 core). A log written to a file or a remote collector costs differently.',
    'series8':'One stack: the API image built from Dockerfile-T (CPython 3.14.10, requirements-T.lock as in series 7 plus httptools 0.9.0, which uvicorn imports only with --http httptools), msgspec-fastapi main (commit in evidence/router-main.json) built on this server as one cp311-abi3 wheel and reached through bench/router-main-shim; PostgreSQL, Redis and Debian images by the same digests as in series 7. Three pairs; percentages of different pairs are never added or multiplied. With 3 pairs a nominal direction by chance is unlikely but possible; effects of the order of the aa-get-fast interval width are reported as uncertain.',
    }
    save(E/'protocol.json',{'pairs':pairs,'random_seed':202611180900,'warmup_seconds':30,'measurement_seconds':60,'blocks_per_pair':8,'points':4*len(schedule),'hashseeds':[2100+i for i in range(8)],'concurrency':WORKERS,'cold_every':COLD_EVERY,'cold_pool':COLD_POOL,'cold_offsets':COLD_OFFSETS,'redis_cpu':0.2,'redis_memory_mib':1024,'api_cpu':1,'generator_cpu':2,'pg_cpu':0.75,'schedule':schedule,'matched_rates':MATCHED_RATES,'route_groups':ROUTE_GROUPS,
      'interpretation_rules':rules,'scope':'Series 8: the method of series 3 and 7 applied to two uvicorn settings of the optimized FastAPI variant (fastapi-msgspec): the HTTP protocol implementation (h11 versus httptools) and the access log (on versus off), plus the aa-get-fast noise reference. Request mix, generator, body verification, estimator and guards as in series 7.','environment':json.loads((E/'host-environment.json').read_text()) if (E/'host-environment.json').exists() else None,'platform_note':'All runtime images and Go executables are amd64. msgspec is the official 0.22.0 PyPI wheel.','environment_guards':'Pinned image IDs and source bindings; own container inventory before/after each point, lifecycle Docker events during main; host isolation guard at main start and end and at post start and end (API, PostgreSQL, Redis, sampler and generator on CPUs 0-3, NUMA node 0; Docker, containerd, user sessions, init and NIC interrupts on CPUs 4-7, NUMA node 1); stack probe (interpreter, package versions, router files, HTTP protocol module and access log lines) per variant in preflight.','duration_selection':'Fixed 30/60 as in series 3 and 7; no result-dependent duration adjustment.','failure_policy':'Any load error, missing telemetry, source/image change, foreign-container event or failed guard stops campaign; no retry or outlier exclusion.'})
    print('protocol written',flush=True)

def headroom():
    try:
        for variant in ('fastapi-msgspec',):
            for i,cpus in enumerate([1,2,2,1]):
                cmd('docker','update','--cpus',str(cpus),GEN)
                point(f'headroom-{variant}-{i}-client{cpus}',variant,2050,15,30,procs=cpus)
        cmd('docker','update','--cpus','2',GEN)
        for i in range(2):point(f'static-{i}','static',2050,15,30)
    finally:
        cmd('docker','update','--cpus','2',GEN)

def static_pre():
    for i in range(2):point(f'static-pre-{i}','static',2050,15,30)

# 0.7 x the minimum smoke RPS of the measured variants, rounded down to a multiple of 100
# (matched-rate-rule.md); set from this server's smoke points before the protocol.
MATCHED_RATES={'get': 400}
def post():
    """After main: client headroom sweep, static controls, equal-load points."""
    host_guard('post-start')
    headroom()
    for rep_ in range(2):
        for group,variants in ROUTE_GROUPS.items():
            for variant in (variants if rep_==0 else variants[::-1]):
                point(f'matched-{rep_}-{variant}-{MATCHED_RATES[group]}',variant,2050,15,30,rate=MATCHED_RATES[group])
    host_guard('post-end')

def cleanup():
    for n in (SAMPLE,API,GEN,PG,REDIS):cmd('docker','rm','-f',n,check=False)
    cmd('docker','network','rm',NET,check=False)
    names=cmd('docker','ps','-a','--format','{{.Names}}').splitlines()
    assert not set((SAMPLE,API,GEN,PG,REDIS))&set(names)
    save(E/'cleanup.json',{'own_resources_removed':True,'remaining_containers':names})

# Build stage (on the server, before setup): image T from Dockerfile-T, then msgspec-fastapi main as
# one cp311-abi3 wheel compiled in the full python:3.14.10 image (same CPython and Debian release as the
# slim base of image T) and installed with regexrs-py into bench/router-main-site for both stacks.
BUILDER='python:3.14.10@sha256:a0362a0fda63931168dbae73de63cdffd798aa7a52c8cf7facaf2aec2c91238e'
SETUPTOOLS='84.2.1'
REGEXRS={'regexrs_py-0.2.0-cp311-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl':'b079d42212745f355aa9fec04d0e2d64256f2c329a94a62842620abcd1f1e4b2',
         'regexrs_py-0.2.0-cp311-abi3-manylinux2014_aarch64.manylinux_2_17_aarch64.whl':'91112e10f279f6a9fa3e97d006d2263a302bc7f4a8f103d6dfce08395e209549'}
TAG_T='msgspec-talk-s8-api-t'
def tree_hashes(root):
    return {str(q.relative_to(root)):hashlib.sha256(q.read_bytes()).hexdigest() for q in sorted(root.rglob('*')) if q.is_file()}
def build():
    site=F/'router-main-site';wheels=R/'build-wheels'
    if site.exists() or wheels.exists() or (E/'image-T.json').exists() or (E/'router-main.json').exists():
        raise RuntimeError('Build outputs exist; inspect before reuse')
    src=json.loads((F/'router-main-src.json').read_text())
    assert tree_hashes(F/'router-main-src')==src['file_sha256'],'router source differs from the recorded commit'
    # BuildKit writes its progress, including every pip download, to stderr.
    b=subprocess.run(['docker','build','--no-cache','--progress=plain','-f',str(F/'service/Dockerfile-T'),'-t',TAG_T,str(F/'service')],text=True,capture_output=True)
    (E/'build-image-T.log').write_text(b.stdout+b.stderr)
    if b.returncode:raise RuntimeError('image T build failed, see evidence/build-image-T.log')
    image=cmd('docker','image','inspect','-f','{{.Id}}',TAG_T).strip()
    run_t=lambda *a:cmd('docker','run','--rm','--network','bridge','--entrypoint',a[0],image,*a[1:])
    image_record={'id':image,'tag':TAG_T,'dockerfile_sha256':hashlib.sha256((F/'service/Dockerfile-T').read_bytes()).hexdigest(),
        'lock_sha256':hashlib.sha256((F/'service/requirements-T.lock').read_bytes()).hexdigest(),
        'python':run_t('python','-c','import platform,sys;print(platform.python_version(),sys.platform,platform.machine())').strip(),
        'pip_freeze':run_t('pip','freeze').splitlines()}
    wheels.mkdir();site.mkdir()
    cmd('docker','run','--rm','-v',str(F/'router-main-src')+':/src:ro','-v',str(wheels)+':/out','--entrypoint','sh',BUILDER,'-ec',
        f'pip install --no-cache-dir --root-user-action=ignore setuptools=={SETUPTOOLS} && cp -r /src /tmp/src && '
        'pip wheel --no-deps --no-build-isolation --no-cache-dir -w /out /tmp/src')
    cmd('docker','run','--rm','-v',str(wheels)+':/out','--entrypoint','pip',image,'download','--no-deps','--only-binary',':all:','--no-cache-dir','-d','/out','regexrs-py==0.2.0')
    built=sorted(q.name for q in wheels.glob('msgspec_fastapi-*.whl'));regexrs=sorted(q.name for q in wheels.glob('regexrs_py-*.whl'))
    assert len(built)==1 and built[0].startswith('msgspec_fastapi-0.0.4-cp311-abi3-linux_'),built
    assert len(regexrs)==1 and hashlib.sha256((wheels/regexrs[0]).read_bytes()).hexdigest()==REGEXRS[regexrs[0]],regexrs
    cmd('docker','run','--rm','-v',str(wheels)+':/wheels:ro','-v',str(site)+':/site','--entrypoint','pip',image,'install','--no-deps','--no-index','--no-compile',
        '--no-cache-dir','--root-user-action=ignore','--target','/site','/wheels/'+built[0],'/wheels/'+regexrs[0])
    save(E/'router-main.json',{'commit':src['commit'],'tree':src['tree'],'builder':BUILDER,'setuptools':SETUPTOOLS,
        'wheels':{n:hashlib.sha256((wheels/n).read_bytes()).hexdigest() for n in built+regexrs},'site_sha256':tree_hashes(site)})
    # Written last: an existing image-T.json marks a completed build.
    save(E/'image-T.json',image_record)
    print('build complete',image,built[0],flush=True)

if __name__=='__main__':
    {'build':build,'setup':setup,'cold':cold,'smoke':smoke,'protocol':make_protocol,'preflight':preflight,'static_pre':static_pre,'main':main,'post':post,'cleanup':cleanup}[sys.argv[1]]()
