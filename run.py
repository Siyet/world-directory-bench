import hashlib, json, os, random, statistics, subprocess, sys, time, urllib.parse, urllib.request
from pathlib import Path

R = Path(__file__).resolve().parent
F = R/'bench'
E = R/'evidence'
API='msgspec-talk-s3-api'; GEN='msgspec-talk-s3-gen'
PG='msgspec-talk-s3-pg'; REDIS='msgspec-talk-s3-redis'
NET='msgspec-talk-s3'; SAMPLE='msgspec-talk-s3-sample'; PORT=18240
# Series 3: 20 closed-loop clients; GET traffic mixes one hot page with a unique cold page
# on every tenth request (about 90% view-cache hits); POST variants send search bodies.
WORKERS=20; COLD_EVERY=10
COLD_POOL=80000; COLD_OFFSETS={'warmup':0,'measurement':25000}
IMAGE='sha256:91fe85483fe0dbb8b9a93420d89ec30c3ea7575e7c7b778e08fd2c6fd6dbc418'
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
    # Cold misses keep up to about a minute of 25 KB pages alive (TTL 60 s); no eviction, a full Redis fails loudly.
    cmd('docker','run','-d','--name',REDIS,'--network',NET,'--network-alias','redis','--cpus','.2','--cpuset-cpus','1','--memory','1024m',REDIS_IMAGE,'redis-server','--save','','--appendonly','no','--maxmemory','900mb','--maxmemory-policy','noeviction')
    for _ in range(100):
        p=subprocess.run(['docker','exec',PG,'pg_isready','-U','directory','-d','directory'],capture_output=True)
        if p.returncode==0:break
        time.sleep(.2)
    else: raise RuntimeError('Database readiness failed')
    data=R.parent/'talk-itgorky-2026-10-17/world-directory/evidence/normalized-v2'
    for module,args in [('directory.init_db',[]),('directory.load',['/data']),('directory.db_size',[])]:
        result=cmd('docker','run','--rm','--name','msgspec-talk-s3-seed','--network',NET,'--cpus','1','--memory','512m','-v',str(F)+':/work:ro','-v',str(data)+':/data:ro','-e','PYTHONPATH=/work/site:/work/service','-e','PYTHONDONTWRITEBYTECODE=1','-e','DATABASE_URL=postgresql+asyncpg://directory:directory-local@postgres/directory','-w','/work/service','--entrypoint','python',IMAGE,'-m',module,*args)
        (E/(module+'.json')).write_text(result)
    cmd('docker','run','-d','--name',GEN,'--network',NET,'--cpus','2','--cpuset-cpus','2,3','--memory','512m','-v',str(F)+':/work:ro','-v',str(R)+':/control:ro','--entrypoint','sleep',DEBIAN,'infinity')
    print('setup complete',flush=True)

def request(path):
    with urllib.request.urlopen(f'http://127.0.0.1:{PORT}'+path,timeout=8) as r:
        return r.status,json.loads(r.read())

# Full optimized stack of the FastAPI chain (schema off, asyncpg, uvloop, view cache) and
# the same set of changes applied to Litestar and Go.
FULL=dict(path='msgspec',output='none',db='asyncpg',loop='uvloop',viewcache='enabled')
# codec json: Cashews json pickler (json.loads + check_repr), then msgspec.convert(strict=True).
# codec msgpack: msgspec.msgpack.encode bytes in Redis, null pickler, msgspec.msgpack.decode(type=Page[City]),
# then the framework encodes the validated page as the JSON response.
# Framework comparison without a cache (every request reads PostgreSQL): standard FastAPI
# (Pydantic schema, SQLAlchemy ORM, asyncio), FastAPI + msgspec + asyncpg + uvloop, Litestar with
# the same msgspec service, asyncpg and uvloop, and Go net/http + pgx.
OPT=dict(path='msgspec',output='none',db='asyncpg',loop='uvloop')
VARIANTS={
'cache':dict(FULL,app='diagnostic_app:app',codec='json'),
'cache-msgpack':dict(FULL,app='diagnostic_app:app',codec='msgpack'),
'pydantic':dict(path='explicit',app='diagnostic_app:app',codec='json'),
# MsgspecRouter with response_model; like FastAPI + Pydantic it trusts an instance of the declared Struct.
'msgspec-schema':dict(path='msgspec',app='diagnostic_app:app',codec='json'),
'pydantic-cache':dict(path='explicit',viewcache='enabled',app='diagnostic_app:app',codec='json'),
# Chain step ORM -> asyncpg: MsgspecRouter without output schema, asyncio; only DB_BACKEND differs.
'msgspec-none-orm':dict(path='msgspec',output='none',app='diagnostic_app:app',codec='json'),
'msgspec-none-asyncpg':dict(path='msgspec',output='none',db='asyncpg',app='diagnostic_app:app',codec='json'),
'fastapi-msgspec':dict(OPT,app='diagnostic_app:app',codec='json'),
'litestar':dict(OPT,app='litestar_asyncpg_app:app',codec='json'),
'go':dict(),
}
# POST /cities/search: the same configurations, with the search body as the input contract.
for _name in ('pydantic','msgspec-schema','fastapi-msgspec'):
    VARIANTS[_name+'-post']=dict(VARIANTS[_name],cases='post')
# POST /cities/validate: a 100-city batch (the hot page items, about 23 KB) as the input contract,
# no database, response {"accepted": 100}. Only the validation library and router differ.
for _name in ('pydantic','msgspec-schema'):
    VARIANTS[_name+'-ingest']=dict(VARIANTS[_name],cases='ingest')
POST_VARIANTS={v for v,x in VARIANTS.items() if x.get('cases') in ('post','ingest')}
GO_LIKE={'go','static'}
CACHED={'cache','cache-msgpack','pydantic-cache'}
GC_PROBE={'cache','cache-msgpack','pydantic','msgspec-schema','pydantic-cache','fastapi-msgspec','msgspec-none-orm','msgspec-none-asyncpg'}
SERIALIZER={'json':{'check_repr':True,'pickler':'JsonPickler'},'msgpack':{'check_repr':False,'pickler':'NonPickler'}}
STORED_PREFIX={'cache':'{','cache-msgpack':'bytes:','pydantic-cache':'{'}

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
        args+=['-e','DATABASE_URL=postgresql+asyncpg://directory:directory-local@postgres/directory','-e','REDIS_URL=redis://redis:6379/0','-e','CACHE_MODE=bypass','-e','VIEW_CACHE='+x.get('viewcache','disabled'),'-e','CACHE_CODEC='+x['codec'],'-e','RESPONSE_PATH='+x['path'],'-e','OUTPUT_MODEL='+x.get('output','schema'),'-e','DB_BACKEND='+x.get('db','orm'),'-e','PYTHONHASHSEED='+str(seed),'-e','PYTHONPATH=/work/site:/work/service:/work/apps','-e','PYTHONDONTWRITEBYTECODE=1','-w','/work/service','--entrypoint','uvicorn',IMAGE,x['app'],'--host','0.0.0.0','--port','8000','--workers','1','--loop',x.get('loop','asyncio'),'--http','h11','--no-access-log']
    cmd(*args)
    for _ in range(100):
        try:
            status,body=request('/cities?limit=100')
            if status==200:
                assert body==json.loads((F/'cases.json').read_text())[0]['expected']
                return
        except (OSError,ValueError):pass
        time.sleep(.2)
    raise RuntimeError('API readiness failed: '+cmd('docker','logs',API,check=False))

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
    assert info[0]['Image']==(DEBIAN if variant in GO_LIKE else IMAGE)
    assert info[1]['Image']==DEBIAN and info[2]['Image']==POSTGRES and info[3]['Image']==REDIS_IMAGE
    expected={API:(1000000000,'0',512*1024**2),GEN:(procs*1000000000,'2,3',512*1024**2),PG:(750000000,'1',1200*1024**2),REDIS:(200000000,'1',1024*1024**2)}
    for obj in info:
        name=obj['Name'].lstrip('/')
        hc=obj['HostConfig']
        assert (hc['NanoCpus'],hc['CpusetCpus'],hc['Memory'])==expected[name],name
    if variant not in GO_LIKE:
        env=dict(x.split('=',1) for x in info[0]['Config']['Env'])
        x=VARIANTS[variant]
        for key,value in {'RESPONSE_PATH':x['path'],'OUTPUT_MODEL':x.get('output','schema'),'DB_BACKEND':x.get('db','orm'),'CACHE_MODE':'bypass','VIEW_CACHE':x.get('viewcache','disabled'),'CACHE_CODEC':x['codec'],'PYTHONHASHSEED':str(seed)}.items():
            assert env[key]==value,(key,env.get(key))
        c=info[0]['Config']['Cmd']
        assert info[0]['Config']['Entrypoint']==['uvicorn'] and c[0]==x['app'] and c[c.index('--loop')+1]==x.get('loop','asyncio') and c[c.index('--http')+1]=='h11',c
    elif variant=='go':
        env=dict(x.split('=',1) for x in info[0]['Config']['Env'])
        assert info[0]['Config']['Entrypoint']==['/work/bin/go-server-full'],info[0]['Config']['Entrypoint']
        assert (env.get('GOMAXPROCS'),env.get('VIEW_CACHE'),env.get('REDIS_ADDR'))==('1','disabled',None),env
    targets={x['Name'].rsplit('-',1)[-1]:x['State']['Pid'] for x in info}
    cmd('docker','run','-d','--name',SAMPLE,'--pid','host','--cgroupns','host','--cpus','.05','--cpuset-cpus','1','--memory','64m','-v',str(R)+':/control:ro','--entrypoint','python',IMAGE,'/control/telemetry.py',json.dumps(targets))
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
    for a,b in zip(samples,samples[1:]):
        assert b['monotonic']>a['monotonic']
        for target in targets:
            assert b['targets'][target]['cpu']['usage_usec']>=a['targets'][target]['cpu']['usage_usec']
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



TIMERS=('apt-daily.timer','apt-daily-upgrade.timer','fwupd-refresh.timer','motd-news.timer','update-notifier-download.timer','man-db.timer','fstrim.timer')
def host_guard(tag):
    """Snapshot processes, interrupts and units, then assert the runtime isolation of CPUs 0-3."""
    irqs={}
    for d in Path('/proc/irq').iterdir():
        if d.is_dir() and (d/'effective_affinity_list').exists():irqs[d.name]=(d/'effective_affinity_list').read_text().strip()
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
    loose=[x for x in procs if 'docker-' not in x['cgroup'] and x['cpus']!='4-5']
    snapshot={'tag':tag,'unix':time.time(),'uptime':Path('/proc/uptime').read_text().split()[0],'affinity':sorted(os.sched_getaffinity(0)),'units':units,'timers':timers,'irq_effective_affinity':irqs,'interrupts':interrupts,'processes':procs,'outside_4_5_non_container':loose}
    save(E/f'host-guard-{tag}.json',snapshot)
    failures=[]
    if not os.sched_getaffinity(0)<={4,5}:failures.append('runner not on CPUs 4-5')
    failures+=[f'{u} {v}' for u,v in units.items() if v!='AllowedCPUs=4-5']
    failures+=[f'{t} {v}' for t,v in timers.items() if v!='inactive']
    if not nic or any(irqs.get(n) not in ('4','5') for n in nic):failures.append(f'NIC interrupts {[(n,irqs.get(n)) for n in nic]}')
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
        if variant not in ('litestar','go'):
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
        result[variant]={'pass':True,'cases':values,'post_cases':posts,'cache':cache,'serializer':probe}
    save(E/'preflight.json',result)
    print('preflight all variants passed',json.dumps({v:r['cache'] for v,r in result.items()}),flush=True)

def psql(sql):
    return cmd('docker','exec',PG,'psql','-U','directory','-d','directory','-At','-F','\t','-c',sql)

def cold():
    """Cold GET pool: unique full pages after random city ids. Expected canonical hashes are
    recorded from two independent implementations (Pydantic + SQLAlchemy ORM, Go + pgx) and must agree."""
    ids=psql('SELECT id FROM cities ORDER BY id').split()
    assert len(ids)>COLD_POOL+200,len(ids)
    rng=random.Random(202610140700)
    chosen=rng.sample(ids[:-101],COLD_POOL)
    pool=[{'path':'/cities','params':{'limit':'100','after_id':i}} for i in chosen]
    save(E/'cold-pool.json',pool)
    recorded={}
    for variant in ('pydantic','go'):
        start(variant,2000)
        p=subprocess.run(['docker','exec',GEN,'/control/bin/http-load','-mode','record','-url','http://api:8000','-cold-cases','/control/evidence/cold-pool.json','-workers','4'],text=True,capture_output=True)
        (E/f'cold-record-{variant}.stderr').write_text(p.stderr)
        assert p.returncode==0,p.stderr[-2000:]
        recorded[variant]=json.loads(p.stdout)
        save(E/f'cold-record-{variant}.json',recorded[variant])
    a,b=recorded['pydantic'],recorded['go']
    assert len(a)==len(b)==COLD_POOL and all(x['canonical_sha256'] for x in a)
    assert [(x['path'],x['params'],x['canonical_sha256']) for x in a]==[(x['path'],x['params'],x['canonical_sha256']) for x in b],'implementations disagree on cold pages'
    assert len({x['canonical_sha256'] for x in a})==COLD_POOL,'cold pages must be distinct'
    (F/'cold-cases.json').write_text(json.dumps(a))
    cmd('docker','rm','-f',API,check=False)
    print('cold pool recorded',len(a),flush=True)

# Search bodies: list filters, ranges and both orders; each returns a full page. Chosen so that
# the PostgreSQL plan stays cheap (EXPLAIN ANALYZE below 0.2 ms on the server, evidence/post-bodies-explain.txt):
# the first smoke with selective bodies (2-40 ms per query) saturated PostgreSQL.
POST_BODIES=[
    {'filter': {'country_codes': ['US', 'IN', 'CN', 'RU', 'BR', 'DE', 'FR', 'IT', 'ES', 'MX', 'JP', 'PH', 'ID', 'PL', 'GB', 'UA', 'TR', 'IR', 'PK', 'NG', 'CO', 'AR', 'CA', 'ZA', 'EG', 'VN', 'TH', 'KR', 'RO', 'NL', 'BE', 'CZ', 'SE', 'AT', 'CH', 'PT', 'HU', 'GR', 'CL', 'PE'], 'feature_codes': ['PPL', 'PPLA', 'PPLA2', 'PPLA3', 'PPLA4']}, 'limit': 100},
    {'filter': {'country_codes': ['US', 'IN', 'CN', 'RU', 'BR', 'DE', 'FR', 'IT', 'ES', 'MX', 'JP', 'PH', 'ID', 'PL', 'GB', 'UA', 'TR', 'IR', 'PK', 'NG', 'CO', 'AR', 'CA', 'ZA', 'EG', 'VN', 'TH', 'KR', 'RO', 'NL', 'BE', 'CZ', 'SE', 'AT', 'CH', 'PT', 'HU', 'GR', 'CL', 'PE'], 'feature_codes': ['PPL', 'PPLA', 'PPLA2', 'PPLA3', 'PPLA4']}, 'order': '-id', 'limit': 100},
    {'filter': {'sources': ['geonames'], 'population': {'min': 1000, 'max': 50000000}}, 'after_id': 'geonames:city:2000000', 'limit': 100},
    {'filter': {'country_codes': ['US', 'IN', 'CN', 'RU', 'BR', 'DE', 'FR', 'IT', 'ES', 'MX', 'JP', 'PH', 'ID', 'PL', 'GB', 'UA', 'TR', 'IR', 'PK', 'NG', 'CO', 'AR', 'CA', 'ZA', 'EG', 'VN', 'TH', 'KR', 'RO', 'NL'], 'sources': ['geonames', 'local'], 'population': {'min': 500}}, 'order': '-id', 'after_id': 'geonames:city:9000000', 'limit': 100},
    {'filter': {'sources': ['geonames'], 'feature_codes': ['PPL', 'PPLA', 'PPLA2', 'PPLA3', 'PPLA4', 'PPLC'], 'population': {'min': 1000}}, 'order': '-id', 'limit': 100},
    {'filter': {'country_codes': ['US', 'IN', 'CN', 'RU', 'BR', 'DE', 'FR', 'IT', 'ES', 'MX', 'JP', 'PH', 'ID', 'PL', 'GB', 'UA', 'TR', 'IR', 'PK', 'NG', 'CO', 'AR', 'CA', 'ZA', 'EG', 'VN', 'TH', 'KR', 'RO', 'NL', 'BE', 'CZ', 'SE', 'AT', 'CH', 'PT', 'HU', 'GR', 'CL', 'PE'], 'sources': ['geonames'], 'feature_codes': ['PPL', 'PPLA', 'PPLA2'], 'population': {'min': 1000, 'max': 20000000}}, 'order': '-id', 'after_id': 'geonames:city:4000000', 'limit': 100},
    {'filter': {'country_codes': ['US', 'IN', 'CN', 'RU', 'BR', 'DE', 'FR', 'IT', 'ES', 'MX', 'JP', 'PH', 'ID', 'PL', 'GB', 'UA', 'TR', 'IR', 'PK', 'NG', 'CO', 'AR', 'CA', 'ZA', 'EG'], 'feature_codes': ['PPL', 'PPLA'], 'population': {'min': 2000}}, 'after_id': 'geonames:city:1000000', 'limit': 100},
    {'filter': {'sources': ['geonames', 'local'], 'feature_codes': ['PPL', 'PPLA', 'PPLA2', 'PPLA3'], 'population': {'min': 500, 'max': 50000000}}, 'after_id': 'geonames:city:7000000', 'limit': 100},
]
CITY_FIELDS=('id','source','name','country_id','division_id','latitude','longitude','population','feature_code')

def reference_search(rows, body):
    """Independent pure Python evaluation of a search body over a full table dump."""
    f=body.get('filter',{});pop=f.get('population') or {}
    out=[r for r in rows if
         ('country_codes' not in f or r['country_code'] in f['country_codes']) and
         ('division_ids' not in f or r['division_id'] in f['division_ids']) and
         ('sources' not in f or r['source'] in f['sources']) and
         ('feature_codes' not in f or r['feature_code'] in f['feature_codes']) and
         (pop.get('min') is None or r['population']>=pop['min']) and
         (pop.get('max') is None or r['population']<=pop['max']) and
         ('name_prefix' not in f or r['name'].startswith(f['name_prefix']))]
    desc=body.get('order','id')=='-id'
    after=body.get('after_id')
    if after is not None:out=[r for r in out if (r['id']<after if desc else r['id']>after)]
    # PostgreSQL text order of ids; C collation is checked below against the database order.
    out.sort(key=lambda r:r['id'].encode(),reverse=desc)
    return [r['id'] for r in out[:body.get('limit',100)]]

def post_cases():
    """Expected bodies for the search cases: the ORM and asyncpg implementations must agree,
    and the returned ids must equal an independent Python evaluation over a table dump."""
    import urllib.error
    raw=psql('SELECT c.id,c.source,c.name,c.division_id,c.population,c.feature_code,k.country_code FROM cities c JOIN countries k ON c.country_id=k.id')
    rows=[dict(zip(('id','source','name','division_id','population','feature_code','country_code'),line.split('\t'))) for line in raw.splitlines()]
    for r in rows:r['population']=int(r['population']);r['division_id']=r['division_id'] or None
    db_order=psql('SELECT id FROM cities ORDER BY id').split()
    assert db_order==sorted(db_order,key=str.encode),'database id order differs from bytewise order'
    bodies={}
    for variant in ('pydantic-post','fastapi-msgspec-post','msgspec-schema-post'):
        start(variant,2000);bodies[variant]=[]
        for body in POST_BODIES:
            req=urllib.request.Request(f'http://127.0.0.1:{PORT}/cities/search',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'},method='POST')
            with urllib.request.urlopen(req,timeout=8) as r:
                assert r.status==200;bodies[variant].append(json.loads(r.read()))
    ref=bodies['pydantic-post']
    assert all(bodies[v]==ref for v in bodies),'search implementations disagree'
    checks=[]
    for body,page in zip(POST_BODIES,ref):
        ids=[x['id'] for x in page['items']]
        assert len(ids)==body['limit'],(body,len(ids))
        assert ids==reference_search(rows,body),body
        assert page['next_after_id']==ids[-1]
        checks.append({'body':body,'items':len(ids),'first':ids[0],'last':ids[-1]})
    cases=[{'path':'/cities/search','method':'POST','body':body,'expected':page} for body,page in zip(POST_BODIES,ref)]
    (F/'post-cases.json').write_text(json.dumps(cases,separators=(',',':'),ensure_ascii=False))
    # Ingest check: the hot page items as one batch; the expected answer counts them.
    hot=json.loads((F/'cases.json').read_text())[0]['expected']
    ingest=[{'path':'/cities/validate','method':'POST','body':{'items':hot['items']},'expected':{'accepted':len(hot['items'])}}]
    (F/'ingest-cases.json').write_text(json.dumps(ingest,separators=(',',':'),ensure_ascii=False))
    for variant in ('pydantic-ingest','msgspec-schema-ingest'):
        start(variant,2000)
        req=urllib.request.Request(f'http://127.0.0.1:{PORT}/cities/validate',data=json.dumps(ingest[0]['body']).encode(),headers={'Content-Type':'application/json'},method='POST')
        with urllib.request.urlopen(req,timeout=8) as r:
            assert r.status==200 and json.loads(r.read())=={'accepted':len(hot['items'])},variant
    save(E/'post-cases-check.json',{'reference_rows':len(rows),'cases':checks,'variants_agree':sorted(bodies)})
    cmd('docker','rm','-f',API,check=False)
    print('post cases written',len(cases),flush=True)

def smoke():
    for variant in VARIANTS:
        point(f'smoke-{variant}',variant,2050,10,20)
    for i in range(2):point(f'smoke-static-{i}','static',2050,15,30)

def make_protocol():
    assert isinstance(MATCHED_RATE,int) and MATCHED_RATE>0,'set MATCHED_RATE from the smoke points first'
    rng=random.Random(202610140900)
    pairs={'pyd-msgspec-schema':{'A':'pydantic','B':'msgspec-schema'},'schema-none':{'A':'msgspec-schema','B':'msgspec-none-orm'},'pyd-none':{'A':'pydantic','B':'msgspec-none-orm'},
      'orm-asyncpg':{'A':'msgspec-none-orm','B':'msgspec-none-asyncpg'},'asyncio-uvloop':{'A':'msgspec-none-asyncpg','B':'fastapi-msgspec'},'uvloop-cache':{'A':'fastapi-msgspec','B':'cache'},
      'fastapi-msgpack-cache':{'A':'cache','B':'cache-msgpack'},'cache-vs-optimizations':{'A':'fastapi-msgspec','B':'pydantic-cache'},'std-msgspec':{'A':'pydantic','B':'fastapi-msgspec'},
      'msgspec-litestar':{'A':'fastapi-msgspec','B':'litestar'},'msgspec-go':{'A':'fastapi-msgspec','B':'go'},
      'post-pyd-msgspec-schema':{'A':'pydantic-post','B':'msgspec-schema-post'},'post-std-msgspec':{'A':'pydantic-post','B':'fastapi-msgspec-post'},
      'ingest-pyd-msgspec':{'A':'pydantic-ingest','B':'msgspec-schema-ingest'}}
    orders={name:rng.sample(['ABBA']*4+['BAAB']*4,8) for name in pairs}
    schedule=[]
    for rnd in range(8):
        names=list(pairs);rng.shuffle(names)
        schedule+=[{'round':rnd,'pair':name,'order':orders[name][rnd]} for name in names]
    sources=[q for q in sorted(F.rglob('*')) if q.is_file() and '__pycache__' not in q.parts]
    save(E/'source-bindings.json',{str(q):hashlib.sha256(q.read_bytes()).hexdigest() for q in sources}|{str(R/n):hashlib.sha256((R/n).read_bytes()).hexdigest() for n in ('run.py','telemetry.py','host_telemetry.py','analyze.py','bin/http-load','matched-rate-rule.md','evidence/cold-pool.json')}|{str(q):hashlib.sha256(q.read_bytes()).hexdigest() for q in sorted((R/'generator').glob('*')) if q.is_file()})
    rules={'lower_bound': 'A variant whose mean API CPU over all its main points is below 0.95 core is reported as an RPS lower bound (client/Redis/latency limited), with API CPU per response as secondary metric, unless the pair is flagged by direction_under_client_limit, where it is the primary metric; never as a server capacity ratio. Classified once per variant.', 'redis': 'Redis keeps 0.1 CPU on cpuset 1 as in the chain series. If Redis throttled time exceeds 1% of the measurement window in any point of a variant, that variant is reported as Redis-limited.', 'pair_ratio': 'The pair ratio is the measured throughput ratio on this client-limited bench (4 closed-loop clients, body verification on 2 generator CPUs); not a capacity ratio. A direction may be claimed only if the nominal interval excludes 0. API CPU per response is reported with the RPS it was measured at, because for the Python variants it falls with load.', 'direction_under_client_limit': 'Generator saturation is anchored to generator CPU in the static controls (static-pre before main, static after main; mean of all four). Static RPS is not used as a limit, because generator cost per response differs between the static Go server and uvicorn responses. If either side of a pair has mean generator CPU >= 0.9 x static generator CPU, the RPS direction of that pair is not reported as a server result; API CPU per response is then the primary metric. Generator CPU per response is reported per variant. Litestar and Go are compared with FastAPI msgspec, not with each other.', 'host_drift': "The host is a VM on the provider's dedicated-CPU plan without other user workloads; per-point steal and softirq shares of CPUs 0-3 are reported and a point with steal above 1% of the window is flagged. Static controls before and after main are compared by RPS, generator CPU per response and API CPU per response; a difference above 10% in RPS or generator CPU per response flags the series as host-drifted (static API CPU per response, about 20 us, is reported but too small for a stable percentage), reported with the results. The per-round values of fastapi-msgspec (present in four pairs, every round) are reported as an in-series drift trace. Per-response CPU values of this series are never set next to values of other series.", 'equal_load': 'After main and headroom, each of cache, cache-msgpack, pydantic-cache, fastapi-msgspec, litestar and go runs two open-loop points at MATCHED_RATE offered RPS (30 s, after 15 s throughput warmup), order as listed then reversed. MATCHED_RATE follows matched-rate-rule.md: 0.7 x the minimum throughput smoke RPS of these six variants on this server, rounded down to a multiple of 100, fixed before this protocol. The standard pydantic variant is excluded because its throughput is below any common rate useful for the others. API CPU per response at this equal load is reported descriptively (no interval) alongside the main-series value, which depends on load. Open-loop mode counts X-Cache hits but does not check content type or a single X-Cache header.', 'manipulation_check': 'Preflight must show stored values with the bytes: prefix and the NonPickler/check_repr off serializer for cache-msgpack, a JSON object and JsonPickler/check_repr on for cache and pydantic-cache; a corrupted entry answered with 500; a valid entry in another encoding (extra space for JSON, other key order for msgpack) answered with 200 and the same body. Variants without a cache answer X-Cache: bypass and write nothing to Redis. In pair fastapi-msgpack-cache the stored format is verified at every point (prefix sample), which is the manipulation check. The CPU estimate is the nominal 95% t interval (7 degrees of freedom) of the per-block log-ratio B/A of API CPU per response, computed like the RPS ratio (analyze.py, comparisons[pair].api_cpu_us_per_response). Three outcomes: the whole interval below 0%, msgpack CPU advantage, stated; the interval contains 0, no detectable difference; the whole interval above 0%, msgpack costlier, stated. In the last two cases the pair stays valid, no msgpack advantage in any metric of this pair (RPS, CPU per response or equal-load values) is claimed, equal-load values are shown as values only, and the Cashews thunder protection from path_disclosure may be named only as a possible explanation from the pre-protocol hot-page diagnostics, not tested in this series.', 'cache_hits': 'Every cached point (cache, cache-msgpack, pydantic-cache) must have <=8 misses and >99.9% hits in window; otherwise campaign stops. Every cached point checks the stored prefix after measurement.', 'validation': 'Cached data validated before every reply: cache: strict msgspec.convert(Page[City]) of json.loads output; cache-msgpack: msgspec.msgpack.decode(type=Page[City]); pydantic-cache: Page[City].model_validate(strict=True) of json.loads output, then FastAPI serializes it through response_model as in the standard app. Without a cache every response is read from PostgreSQL and passes the same DTO and contract preparation as in the chain.', 'claim_scope': 'Pair fastapi-msgpack-cache: storing msgpack bytes and decoding them with msgspec is a change of what the view cache stores; not presented as a Cashews setting. Pair cache-vs-optimizations: tests whether a view cache on the unoptimized standard FastAPI app (Pydantic schema, SQLAlchemy ORM, asyncio) outperforms all chain optimizations without a cache on this single hot GET with ~100% hits; the result is specific to this hit rate and query. The statement that the cache outperforms the optimizations is made only if the RPS interval of this pair excludes 0 in favour of B, the pair is not client-limited, and API CPU per response at equal load is also lower for B at both matched points (the higher B point below the lower A point); analyze.py records the three conditions in claim_scope_cache_vs_optimizations; the no-cache side may idle a few percent waiting for PostgreSQL with 4 closed-loop clients, so RPS alone can overstate the cache advantage. Any RPS statement for a variant classified as a lower bound says so.', 'headroom': 'After main, the client 1/2/2/1 CPU sweep runs for go, litestar and fastapi-msgspec, plus static controls.', 'path_disclosure': 'Framework pairs read PostgreSQL on every request: FastAPI msgspec variant uses MsgspecRouter without output schema, direct asyncpg and uvloop; Litestar uses the same service, asyncpg repository and uvloop with hand-parsed query parameters and the same msgspec conversion, and encodes the prepared Struct page; Go uses net/http, database/sql with pgx and stdlib encoding/json with explicit field checks. The standard variant is FastAPI with Pydantic response_model, SQLAlchemy ORM and asyncio. Pair fastapi-msgpack-cache bundles four changes: msgpack instead of JSON storage (including the miss-path encode), no json.loads, no repr, msgpack decode instead of convert. Generator CPU per response differs by server type, so closed-loop RPS against Go includes a client-side effect.'}
    rules['equal_load']=rules['equal_load'].replace('MATCHED_RATE offered','%d offered'%MATCHED_RATE,1)
    # Series 3 overrides.
    rules['redis']='Redis gets 0.2 CPU and 1024 MiB on cpuset 1 (maxmemory 900mb, noeviction), because cold misses keep up to a minute of 25 KB pages. If Redis throttled time exceeds 1% of the measurement window in any point of a variant, that variant is reported as Redis-limited.'
    rules['pair_ratio']='The pair ratio is the measured throughput ratio with 20 closed-loop clients and memoized body verification on 2 generator CPUs; not a capacity ratio. A direction may be claimed only if the nominal interval excludes 0. The same rule applies to API CPU per response (comparisons[pair].api_cpu_us_per_response, same estimator): a CPU direction is stated only if its interval excludes 0. The primary metric of each pair is fixed in advance: RPS, or API CPU per response for pairs flagged by direction_under_client_limit; the other metric is descriptive and never replaces a null primary result. When neither side is a lower bound (API at least 0.95 core on both), the CPU interval mirrors the RPS interval and is not independent evidence. API CPU per response is reported with the RPS it was measured at; for a lower-bound side it is compared at different loads. For fastapi-msgpack-cache, manipulation_check fixes the primary metric (API CPU per response) and its outcomes; the RPS of that pair stays a secondary metric under this rule, and an RPS direction is stated only if its interval excludes 0, the pair is not flagged by direction_under_client_limit and the CPU outcome is an advantage. analyze.py records the decision per pair: primary_metric, primary_direction_claim_allowed, rps_direction_claim_allowed, cpu_compared_at_different_loads (a side is a lower bound) and cpu_interval_mirrors_rps (neither side is a lower bound).'
    rules['cache_hits']='Every cached point (cache, cache-msgpack, pydantic-cache) must have an in-window hit share within [0.88, 0.905], every cold request answered with X-Cache: miss, and at most 3 x 20 hot-page misses (expiry of the 60 s entry with concurrent clients); otherwise the campaign stops. Every cached point checks the stored prefix of a sample of entries after measurement.'
    rules['request_mix']='GET variants: every tenth request (global counter) is a unique cold page from the pool, taken sequentially from offset 0 in the warmup run and from offset 25000 in the measurement run (pool 80000) of each point; an exhausted pool is a client error. Variants without a cache serve the same mix from PostgreSQL. POST search variants cycle through eight search bodies; ingest variants send one batch case; no cache in either. Static controls use only the hot page.'
    rules['post']='POST search pairs use the GET configurations with the search body as input; the response contract (Page[City] JSON body) is the same on both sides. Their search bodies are small (about 120-400 bytes), so the share of input decoding is not identifiable from them: they also change the output validation and serialization path (Pydantic response_model vs msgspec typed output; in post-std-msgspec output validation is off, OUTPUT_MODEL=none, and the repository and event loop change too), the SQL differs from GET, and the BodyLimits scan of every search body costs more than the decoding itself. The input contract is isolated by ingest-pyd-msgspec: a 23 KB batch decoded and validated by Pydantic through FastAPI (json.loads, then validation) or by the msgspec router (msgspec Decoder), no database, a constant small response; the BodyLimits per-byte scan is skipped on that route only (size, media type and UTF-8 checks remain), so NaN or duplicate keys in an invalid batch may be classified differently by the two paths; preflight compares valid and selected invalid batches. The route has no cache semantics and sends no X-Cache header (checked in preflight), so the generator runs ingest points without an X-Cache expectation; every response body is still verified. The difference between POST and GET API CPU per response of the same variant is reported descriptively (same series), never as a sum or difference of pair percentages.'
    rules['host_drift']=rules['host_drift'].replace('present in four pairs','present in six pairs')
    rules['path_disclosure']+=' Cashews @cache keeps its default protected=True (thunder protection): concurrent calls with the same key share one backend read and one deserialization. Diagnostics before this protocol (evidence/redisdiag-*, hot page only): 3279 Redis GET for 65345 responses with 20 clients, 12931 for 51713 with 4 clients. So on the JSON cache path json.loads and the repr check are shared by concurrent hits of the hot page, while validation in the handler (msgspec.convert or msgspec.msgpack.decode with the type, Pydantic model_validate) runs for every response. In the same diagnostics the JSON-versus-msgpack cache difference fell from about 12% of API CPU per response with 4 clients to none with 20 clients (hot page only, evidence/diag-*, gcoff-*), also with the cyclic GC disabled. X-Cache is set to hit before the cached call, so requests that wait on a coalesced miss are labelled hit, although they did not read Redis themselves; only the first caller is labelled miss.'
    rules['direction_under_client_limit']+=' Series 3: the static control is not generator-saturated (static-pre: generator about 1.5-1.6 of its 2 cores, API about 0.9 core at about 42k RPS, no throttling; what limits it was not identified, the static server runs with GOMAXPROCS=1 at about 0.9 core), so the 0.9 x static generator CPU threshold is conservative rather than a ceiling.'
    rules['claim_scope']=rules['claim_scope'].replace('on this single hot GET with ~100% hits','on GET traffic with about 90% hits (one hot page, unique cold pages)').replace('with 4 closed-loop clients','with 20 closed-loop clients')
    rules['equal_load']=rules['equal_load'].replace('Open-loop mode counts X-Cache hits but does not check content type or a single X-Cache header.','Open-loop mode uses the same request mix and counts X-Cache values by request kind but does not check content type or a single X-Cache header.')
    rules['path_disclosure']+=' POST variants: FastAPI with the same app; Pydantic parses the body through FastAPI (json.loads, then validation), the msgspec router decodes the raw body with a msgspec Decoder; BodyLimits middleware scans every POST body in both.'

    save(E/'protocol.json',{'pairs':pairs,'random_seed':202610140900,'warmup_seconds':30,'measurement_seconds':60,'blocks_per_pair':8,'points':4*len(schedule),'hashseeds':[2100+i for i in range(8)],'concurrency':WORKERS,'cold_every':COLD_EVERY,'cold_pool':COLD_POOL,'cold_offsets':COLD_OFFSETS,'redis_cpu':0.2,'redis_memory_mib':1024,'api_cpu':1,'generator_cpu':2,'pg_cpu':0.75,'cache_ttl_seconds':60,'schedule':schedule,'matched_rate':MATCHED_RATE,
      'interpretation_rules':rules,**{'scope': 'Series 3 on the same dedicated server, stack, durations, estimator and guards as the second series (talk-srv-msgcache-20261013), with three changes requested by the user: 20 closed-loop clients instead of 4; GET traffic with about 90% view-cache hits (every tenth request asks for a unique full page after a random city id, never repeated within a generator process, all other requests ask for the hot page /cities?limit=100); and POST requests with JSON bodies as input contracts: /cities/search with a search body (list filters, a population range, order and keyset pagination; the eight bodies were chosen with cheap PostgreSQL plans after the first POST smoke with selective bodies saturated PostgreSQL, see evidence/superseded-post1 and post-bodies-explain.txt), and /cities/validate with a 100-city batch (the hot page items, about 23 KB) that is only decoded and validated, without database access. The generator verifies every 200 body either fully against the independent expected value or by byte identity with a body of the same case that already passed the full check in the same process (memoized verification; generator CPU per response fell from about 930 us in series 2 to well below the API for the Python variants, about 185 us with Go, mostly canonical hashing of cold pages); cold pages are checked by a canonical SHA-256 recorded from two independent implementations (Pydantic + SQLAlchemy ORM and Go + pgx) that agreed on all pages. Fourteen pairs: the whole FastAPI chain (pyd-msgspec-schema, schema-none, pyd-none, orm-asyncpg, asyncio-uvloop, uvloop-cache, fastapi-msgpack-cache), cache-vs-optimizations, std-msgspec, msgspec-litestar, msgspec-go, two POST search pairs (post-pyd-msgspec-schema, post-std-msgspec) whose variants are the GET configurations with the search body, and ingest-pyd-msgspec (Pydantic -> msgspec, the batch check). All values of this series are reported as series 3 and never set next to values of other series without a label.', 'environment': 'Timeweb Cloud msk-1, Dedicated High CPU, 6 vCPU (KVM, lscpu: AMD EPYC-Rome), 8 GB, Ubuntu 24.04.5, kernel 6.8, Docker 29.1.3, cgroup v2 with the systemd driver. Benchmark containers pinned: API cpuset 0, PostgreSQL/Redis/sampler cpuset 1, generator cpuset 2-3; docker/containerd, all running, timer-activated and known on-demand systemd services, user sessions and NIC interrupts restricted to CPUs 4-5 at runtime (system.slice itself stays 0-5 because the container scopes live there); apt, fwupd, motd-news, man-db and fstrim timers stopped for the campaign. No other user workloads; the zabbix agent of the provider and sysstat run on CPUs 4-5. Host isolation is snapshotted and then asserted at main start and end and at post start and end (host-guard-*.json). static_pre and post run without the host-telemetry and container-event monitors; their own container inventory guard per point still applies.', 'platform_note': 'All runtime images and Go executables are amd64. msgspec is the official 0.22.0 PyPI wheel (the Mac series used a post-release snapshot labelled 0.22.0 that also contained the #1130 _eval_type change and a vendored PCRE2 pattern engine; the benchmark schemas have no pattern constraints); other Python packages are the same versions as the Mac site.', 'environment_guards': 'Pinned image IDs and source bindings; own container inventory before/after each point, lifecycle Docker events during main; host isolation guard at main start and end and at post start and end. Host aggregate and per-CPU ticks (including steal) every 10 s, per-CPU /proc/stat at 1 Hz in the sampler. PSI/RPS are diagnostics, not rejection thresholds.', 'telemetry': '1Hz, API init-process RSS; window selected by generator Unix timestamps; realtime/monotonic differences audited, boundary uncertainty reported if clock steps. RPS and bins monotonic.', 'duration_selection': 'Fixed conservative 30/60 as in the Mac series; no result-dependent duration adjustment.', 'failure_policy': 'Any load error, missing telemetry, source/image change, foreign-container event or failed guard stops campaign; no retry or outlier exclusion.'}})
    print('protocol written',flush=True)

def headroom():
    try:
        for variant in ('go','litestar','fastapi-msgspec'):
            for i,cpus in enumerate([1,2,2,1]):
                cmd('docker','update','--cpus',str(cpus),GEN)
                point(f'headroom-{variant}-{i}-client{cpus}',variant,2050,15,30,procs=cpus)
        cmd('docker','update','--cpus','2',GEN)
        for i in range(2):point(f'static-{i}','static',2050,15,30)
    finally:
        cmd('docker','update','--cpus','2',GEN)

def static_pre():
    for i in range(2):point(f'static-pre-{i}','static',2050,15,30)

MATCHED_RATE=800  # 0.7 x min series 3 smoke RPS of the six equal-load variants (fastapi-msgspec 1196.85) = 837.8, rounded down to 800
def post():
    """After main: client headroom sweep, static controls, equal-load points."""
    host_guard('post-start')
    headroom()
    order=['cache','cache-msgpack','pydantic-cache','fastapi-msgspec','litestar','go']
    for rep_ in range(2):
        for variant in (order if rep_==0 else order[::-1]):
            point(f'matched-{rep_}-{variant}-{MATCHED_RATE}',variant,2050,15,30,rate=MATCHED_RATE)
    host_guard('post-end')

def cleanup():
    for n in (SAMPLE,API,GEN,PG,REDIS):cmd('docker','rm','-f',n,check=False)
    cmd('docker','network','rm',NET,check=False)
    names=cmd('docker','ps','-a','--format','{{.Names}}').splitlines()
    assert not set((SAMPLE,API,GEN,PG,REDIS))&set(names)
    save(E/'cleanup.json',{'own_resources_removed':True,'remaining_containers':names})

if __name__=='__main__':
    {'setup':setup,'cold':cold,'postcases':post_cases,'smoke':smoke,'protocol':make_protocol,'preflight':preflight,'static_pre':static_pre,'main':main,'post':post,'cleanup':cleanup}[sys.argv[1]]()
