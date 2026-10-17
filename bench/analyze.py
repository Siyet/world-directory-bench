import json,statistics
from pathlib import Path
R=Path(__file__).resolve().parent
rows=json.loads((R/'evidence/results.json').read_text())
by={}
for r in rows:
    result=r['result']
    assert all(result[k]['errors']==0 for k in ('warmup','in_window','drain')),(r['variant'],result)
    selected=[x for x in r['memory_samples'] if result['measurement_start_unix']<=x['time']<result['measurement_end_unix']]
    assert len(selected)>=40,(r['variant'],len(selected))
    rss=[x['VmRSS']/1024 for x in selected]
    hwm=[x['VmHWM']/1024 for x in selected]
    point={'round':r['round'],'rps':result['goodput_rps'],'rss_mib':statistics.median(rss),'rss_min_mib':min(rss),'rss_max_mib':max(rss),'hwm_mib':max(hwm),'baseline_rss_mib':r['baseline']['VmRSS']/1024,'samples':len(selected),'cpu':{}}
    for name,a in r['after'].items():
        b=r['before'][name];point['cpu'][name]=(a['usage_usec']-b['usage_usec'])/1e6/(a['time']-b['time'])
    by.setdefault(r['variant'],[]).append(point)
summary={'scope':'local Linux ARM64 Docker; API main-process RSS only, not allocations or total stack; eight fresh-process rounds per variant; closed-loop C4 correct throughput','variants':{},'comparisons':{}}
for name,points in by.items():
    fields=['rps','rss_mib','rss_min_mib','rss_max_mib','hwm_mib','baseline_rss_mib']
    values={f:{'median':statistics.median(p[f] for p in points),'min':min(p[f] for p in points),'max':max(p[f] for p in points)} for f in fields}
    summary['variants'][name]={'rounds':len(points),**values,'points':points}
pairs=[('pydantic','msgspec-strict'),('pydantic','msgspec-none'),('msgspec-strict','msgspec-none'),('msgspec-none','asyncpg'),('asyncpg','uvloop'),('uvloop','cache'),('msgspec-strict','litestar'),('msgspec-strict','go')]
for before,after in pairs:
    aa={p['round']:p for p in by[before]};bb={p['round']:p for p in by[after]}
    rounds=sorted(aa.keys()&bb.keys())
    rps=[100*(bb[i]['rps']/aa[i]['rps']-1) for i in rounds]
    rss=[100*(bb[i]['rss_mib']/aa[i]['rss_mib']-1) for i in rounds]
    summary['comparisons'][before+'->'+after]={'rounds':len(rounds),'ratio_of_median_rps_percent':100*(statistics.median(bb[i]['rps'] for i in rounds)/statistics.median(aa[i]['rps'] for i in rounds)-1),'ratio_of_median_rss_percent':100*(statistics.median(bb[i]['rss_mib'] for i in rounds)/statistics.median(aa[i]['rss_mib'] for i in rounds)-1),'paired_rps_percent':{'median':statistics.median(rps),'min':min(rps),'max':max(rps),'positive_rounds':sum(x>0 for x in rps),'all':rps},'paired_rss_percent':{'median':statistics.median(rss),'min':min(rss),'max':max(rss),'all':rss}}
(R/'evidence/summary.json').write_text(json.dumps(summary,indent=2))
print(json.dumps({k:{'rps':v['rps'],'rss_mib':v['rss_mib']} for k,v in summary['variants'].items()},indent=2))
