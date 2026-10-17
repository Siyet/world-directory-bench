from run import *
DATA=R.parents[1]/'talk-itgorky-2026-10-17/world-directory/evidence/normalized-v2'
cmd('docker','network','create',NET)
cmd('docker','run','-d','--name',PG,'--network',NET,'--network-alias','postgres','--cpus','.75','--cpuset-cpus','1','--memory','1200m','--tmpfs','/var/lib/postgresql:size=900m','-e','POSTGRES_USER=directory','-e','POSTGRES_PASSWORD=directory-local','-e','POSTGRES_DB=directory','postgres:18','postgres','-c','max_connections=40','-c','shared_buffers=64MB','-c','max_wal_size=256MB')
cmd('docker','run','-d','--name','msgspec-talk-v15-redis','--network',NET,'--network-alias','redis','--cpus','.1','--cpuset-cpus','1','--memory','128m','redis:7-alpine','redis-server','--save','','--appendonly','no')
for _ in range(100):
 p=subprocess.run(['docker','exec',PG,'pg_isready','-U','directory','-d','directory'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 if p.returncode==0:break
 time.sleep(.2)
for module,args in [('directory.init_db',[]),('directory.load',['/data']),('directory.db_size',[])]:
 result=cmd('docker','run','--rm','--name','msgspec-talk-v15-seed','--network',NET,'--cpus','1','--memory','512m','-v',str(R)+':/work:ro','-v',str(DATA)+':/data:ro','-e','PYTHONPATH=/work/site:/work/service','-e','PYTHONDONTWRITEBYTECODE=1','-e','DATABASE_URL=postgresql+asyncpg://directory:directory-local@postgres/directory','-w','/work/service','--entrypoint','python','world-directory-service-load-api:latest','-m',module,*args)
 (R/('evidence/'+module+'.json')).write_text(result)
 print(module,result,flush=True)
