import json,subprocess,time,sys,urllib.request,urllib.error,hashlib,statistics
from pathlib import Path
R=Path(__file__).resolve().parent
API='msgspec-talk-v15-api';GEN='msgspec-talk-v15-gen';PG='msgspec-talk-v15-postgres';NET='msgspec-talk-v15';SAMPLE='msgspec-talk-v15-sample';PORT=18215
VARIANTS={
'pydantic':dict(path='explicit'),
'msgspec-strict':dict(path='msgspec'),
'msgspec-none':dict(path='msgspec',output='none'),
'asyncpg':dict(path='msgspec',output='none',db='asyncpg'),
'uvloop':dict(path='msgspec',output='none',db='asyncpg',loop='uvloop'),
'cache':dict(path='msgspec',output='none',db='asyncpg',loop='uvloop',viewcache='enabled'),
'litestar':dict(path='msgspec'),
'go':dict(),
}
variants=list(VARIANTS)
# Even-n Williams design: every variant occupies every position once and every ordered carryover occurs once.
base=[0,1,7,2,6,3,5,4]
orders=[[variants[(x+i)%8] for x in base] for i in range(8)]
def cmd(*args,check=True):
 p=subprocess.run(args,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 if check and p.returncode:raise RuntimeError(str(args)+'\n'+p.stderr)
 return p.stdout

def request(path):
 try:
  with urllib.request.urlopen(f'http://127.0.0.1:{PORT}'+path,timeout=8) as r:return r.status,dict(r.headers),json.loads(r.read())
 except urllib.error.HTTPError as r:return r.code,dict(r.headers),json.loads(r.read())

def start(v):
 cmd('docker','rm','-f',API,check=False)
 args=['docker','run','-d','--name',API,'--network',NET,'--network-alias','api','--cpus','1','--cpuset-cpus','0','--memory','512m','-p',f'127.0.0.1:{PORT}:8000','-v',str(R)+':/work:ro']
 if v=='static':
  args+=['-e','GOMAXPROCS=1','--entrypoint','/work/bin/static','debian:12-slim','-body','/work/body.json','-sha256',hashlib.sha256((R/'body.json').read_bytes()).hexdigest()]
 elif v=='go':
  args+=['-e','GOMAXPROCS=1','-e','DATABASE_URL=postgres://directory:directory-local@postgres/directory','--entrypoint','/work/bin/go-server','debian:12-slim']
 else:
  x=VARIANTS[v]
  args+=['-e','DATABASE_URL=postgresql+asyncpg://directory:directory-local@postgres/directory','-e','REDIS_URL=redis://redis:6379/0','-e','CACHE_MODE=bypass','-e','VIEW_CACHE='+x.get('viewcache','disabled'),'-e','RESPONSE_PATH='+x['path'],'-e','OUTPUT_MODEL='+x.get('output','schema'),'-e','DB_BACKEND='+x.get('db','orm'),'-e','PYTHONPATH=/work/site:/work/service:/work/apps','-e','PYTHONDONTWRITEBYTECODE=1','-w','/work/service','--entrypoint','uvicorn','world-directory-service-load-api:latest',('litestar_app:app' if v=='litestar' else 'diagnostic_app:app'),'--host','0.0.0.0','--port','8000','--workers','1','--loop',x.get('loop','asyncio'),'--http','h11','--no-access-log']
 cmd(*args)
 for _ in range(100):
  try:
   status,headers,body=request('/cities?limit=100')
   if status==200:return
  except Exception:pass
  time.sleep(.2)
 raise RuntimeError(cmd('docker','logs',API))

def metrics(name):
 raw=cmd('docker','exec',name,'sh','-c','cat /sys/fs/cgroup/cpu.stat; cat /proc/1/status; cat /sys/fs/cgroup/memory.current')
 result={'time':time.time()}
 for line in raw.splitlines():
  fields=line.replace(':','').split()
  if fields and fields[0] in {'usage_usec','nr_throttled','throttled_usec','VmRSS','VmHWM'}:result[fields[0]]=int(fields[1])
 result['cgroup_memory_current']=int(raw.splitlines()[-1])
 return result
attempt=0
def load(seconds,cache='bypass'):
 global attempt
 attempt+=1
 args=('docker','exec',GEN,'/work/bin/http-load','-mode','throughput','-url','http://api:8000','-cases','/work/cases.json','-workers','4','-duration',str(seconds),'-warmup','2','-expected-cache',cache,'-gomaxprocs','2')
 started=time.time();p=subprocess.run(args,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE);finished=time.time()
 record={'args':args,'started':started,'finished':finished,'exit_code':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
 (R/f'evidence/load-attempt-{attempt:03}.json').write_text(json.dumps(record,indent=2))
 if p.returncode:raise RuntimeError(p.stdout+'\n'+p.stderr)
 return json.loads(p.stdout),record

def preflight():
 paths=['/cities?limit=100','/cities?limit=1','/cities?limit=0','/cities?limit=1001','/cities?limit=x','/cities?unknown=x','/cities?country_code=RU&limit=3','/cities?min_population=1000000&limit=3','/cities?name_prefix=M&limit=3','/cities?limit=%2B1','/cities?limit=1&limit=2']
 baseline=None;result={}
 expected=json.loads((R/'cases.json').read_text())[0]['expected']
 for v in variants:
  start(v);values={}
  for path in paths:
   status,headers,body=request(path);values[path]={'status':status,'body':body}
  assert values[paths[0]]['body']==expected,v
  if baseline is None:baseline=values
  else:assert values==baseline,(v,[(k,values[k],baseline[k]) for k in paths if values[k]!=baseline[k]])
  result[v]={'pass':True,'cases':len(paths)}
 (R/'evidence/preflight.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)

def run():
 protocol={'orders':orders,'variants':VARIANTS,'seconds':10,'preconditioning_seconds':3,'generator_warmup_rounds_per_worker':2,'concurrency':4,'api_cpus':1,'generator_cpus':2,'scope':'local Linux ARM64 Docker, PG tmpfs, one hot GET /cities?limit=100 key','memory':'API main-process VmRSS sampled every200ms in separate PID-sharing container. VmHWM startup-inclusive reported separately. Cgroup memory snapshots diagnostic only. Measured-window timestamps supplied by generator; only samples within this exact interval are analyzed.'}
 (R/'evidence/protocol.json').write_text(json.dumps(protocol,indent=2))
 cmd('docker','run','-d','--name',GEN,'--network',NET,'--cpus','2','--cpuset-cpus','2,3','--memory','512m','-v',str(R)+':/work:ro','--entrypoint','sleep','debian:12-slim','infinity')
 start('static');load(3);static,_=load(5);(R/'evidence/static-before.json').write_text(json.dumps(static,indent=2))
 rows=[]
 for round_index,order in enumerate(orders):
  for v in order:
   if v=='cache':cmd('docker','exec','msgspec-talk-v15-redis','redis-cli','FLUSHDB')
   start(v);baseline=metrics(API)
   cache='hit' if v=='cache' else 'bypass'
   preconditioning,_=load(3,cache)
   cmd('docker','run','-d','--name',SAMPLE,'--pid','container:'+API,'--cpus','.05','--cpuset-cpus','1','--memory','64m','-v',str(R)+':/work:ro','--entrypoint','python','world-directory-service-load-api:latest','/work/sample.py')
   sampler_before=metrics(SAMPLE)
   (R/f'evidence/sampler-{round_index}-{v}.json').write_text(cmd('docker','inspect',SAMPLE))
   before={n:metrics(n) for n in [API,GEN,PG]}
   result,execution=load(10,cache)
   after={n:metrics(n) for n in [API,GEN,PG]}
   sampler_after=metrics(SAMPLE)
   cmd('docker','stop','-t','1',SAMPLE)
   samples=[json.loads(x) for x in cmd('docker','logs',SAMPLE).splitlines()]
   cmd('docker','rm',SAMPLE)
   row={'round':round_index,'variant':v,'result':result,'preconditioning':preconditioning,'baseline':baseline,'before':before,'after':after,'execution':execution,'memory_samples':samples,'sampler_before':sampler_before,'sampler_after':sampler_after}
   rows.append(row);(R/'evidence/results.json').write_text(json.dumps(rows,indent=2))
   (R/f'evidence/inspect-{round_index}-{v}.json').write_text(cmd('docker','inspect',API,GEN,PG))
   print(json.dumps({'round':round_index,'variant':v,'result':result}),flush=True)
 start('static');load(3);static,_=load(5);(R/'evidence/static-after.json').write_text(json.dumps(static,indent=2))
 bindings=json.loads((R/'evidence/source-bindings.json').read_text())
 assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in bindings.items())
 (R/'evidence/complete.json').write_text(json.dumps({'points':len(rows)}))
if __name__=='__main__':
 if sys.argv[1]=='preflight':preflight()
 elif sys.argv[1]=='run':run()
