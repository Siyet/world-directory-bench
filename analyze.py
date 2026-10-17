import json,math,statistics
from pathlib import Path
R=Path(__file__).resolve().parent;E=R/'evidence'
def read(p):return json.loads(p.read_text())
CONTROL='fastapi-msgspec'
def estimate(values):
    m=statistics.mean(values); margin=2.3646242515927844*statistics.stdev(values)/math.sqrt(8)
    return {'percent':100*math.expm1(m),'nominal_95pct_t_interval':[100*math.expm1(m-margin),100*math.expm1(m+margin)],'blocks':8}
FIELDS=('user','nice','system','idle','iowait','irq','softirq','steal')
def cpu_lines(text):
    return {l.split()[0]:dict(zip(FIELDS,map(int,l.split()[1:9]))) for l in text.splitlines() if l.startswith('cpu')}
def cpu_shares(a,z):
    """Per-CPU shares of the window from the sampler's host /proc/stat."""
    x,y=cpu_lines(a['vm']['cpu_stat']),cpu_lines(z['vm']['cpu_stat']);out={}
    for c in ('cpu0','cpu1','cpu2','cpu3','cpu4','cpu5'):
        d={k:y[c][k]-x[c][k] for k in FIELDS};total=sum(d.values())
        out[c]={'steal':d['steal']/total,'softirq':d['softirq']/total,'busy':1-(d['idle']+d['iowait'])/total}
    return out
STEAL_FLAG=0.01
def host_summary():
    path=E/'host-telemetry.jsonl'
    if not path.exists():return None
    rows=[json.loads(l) for l in path.read_text().splitlines()]
    a,z=rows[0],rows[-1];out={'samples':len(rows),'seconds':z['monotonic']-a['monotonic'],'per_cpu':{}}
    for c in a['per_cpu_ticks']:
        d={k:z['per_cpu_ticks'][c][k]-a['per_cpu_ticks'][c][k] for k in FIELDS};t=sum(d.values())
        out['per_cpu'][c]={'steal':d['steal']/t,'softirq':d['softirq']/t,'busy':1-(d['idle']+d['iowait'])/t}
    # Per benchmark CPU, so that a burst on one CPU is not diluted by the others.
    out['max_interval_steal_share']={c:max(((r2['per_cpu_ticks'][c]['steal']-r1['per_cpu_ticks'][c]['steal'])/max(1,sum(r2['per_cpu_ticks'][c][k]-r1['per_cpu_ticks'][c][k] for k in FIELDS)) for r1,r2 in zip(rows,rows[1:])),default=None) for c in ('cpu0','cpu1','cpu2','cpu3')}
    return out
def main():
    protocol=read(E/'protocol.json');out={'comparisons':{},'scope':protocol,'complete':False}
    for pair,variants in protocol['pairs'].items():
        points=[];blocks=[]
        for item in [x for x in protocol['schedule'] if x['pair']==pair]:
            block=[]
            for pos,side in enumerate(item['order']):
                path=E/f"main-{item['round']:02}-{pair}-{pos}-{side}"
                raw=read(path/'result.json');r=raw['result']
                assert raw['variant']==variants[side] and raw['seed']==2100+item['round']
                assert r['duration_seconds']==60
                assert all(raw[phase][window]['errors']==0 for phase in ('warmup','result') for window in ('warmup','in_window','drain'))
                bins=r['per_second'];assert len(bins)==60
                successes=r['in_window']['terminal_counts']['success']
                assert sum(b['successes'] for b in bins)==successes
                assert abs(r['goodput_rps']-successes/60)<1e-10
                samples=[json.loads(line) for line in (path/'telemetry.jsonl').read_text().splitlines()]
                measured=[s for s in samples if r['measurement_start_unix']<=s['unix']<r['measurement_end_unix']]
                assert len(measured)>=54
                a,z=measured[0],measured[-1];dt=z['monotonic']-a['monotonic']
                cpu={n:(z['targets'][n]['cpu']['usage_usec']-a['targets'][n]['cpu']['usage_usec'])/1e6/dt for n in a['targets']}
                rss=statistics.median(s['targets']['api']['container_init_rss_kib']/1024 for s in measured)
                clock_steps=[(b['unix']-a['unix'])-(b['monotonic']-a['monotonic']) for a,b in zip(samples,samples[1:])]
                p={'path':path.name,'round':item['round'],'position':pos,'side':side,'variant':variants[side],'rps':r['goodput_rps'],'rss_mib':rss,'cpu_cores':cpu,'checked_successes':successes,'cache':r['in_window']['cache'],'first_last_half_change_percent':100*(sum(b['successes'] for b in bins[30:])/sum(b['successes'] for b in bins[:30])-1),'max_abs_realtime_step_warmup_and_measurement_seconds':max(map(abs,clock_steps)),'telemetry_samples':len(measured),'check_wall_seconds':sum(b['check_elapsed_seconds'] for b in bins),'api_throttled_usec':z['targets']['api']['cpu']['throttled_usec']-a['targets']['api']['cpu']['throttled_usec']}
                p['throttled_usec']={n:z['targets'][n]['cpu']['throttled_usec']-a['targets'][n]['cpu']['throttled_usec'] for n in a['targets']}
                p['throttled_share_of_window']={n:v/1e6/dt for n,v in p['throttled_usec'].items()}
                p['api_cpu_us_per_response']=cpu['api']*1e6/p['rps']
                if variants[side] in ('cache','cache-msgpack','pydantic-cache'):
                    c=p['cache'];k=r['in_window']['cache_by_kind']
                    assert 0.88<=c.get('hit',0)/(c.get('hit',0)+c.get('miss',0))<=0.905 and set(k.get('cold',{}))<={'miss'} and k['hot'].get('miss',0)<=60,(path.name,c,k)
                    p['hit_share']=c.get('hit',0)/(c.get('hit',0)+c.get('miss',0));p['cache_by_kind']=k
                p['cpu_shares']=cpu_shares(a,z)
                points.append(p);block.append(p)
            blocks.append({'round':item['round'],'order':item['order'],**{name:sum((1 if p['side']=='B' else -1)*math.log(p[key]) for p in block)/2 for name,key in [('rps_log_ratio','rps'),('rss_log_ratio','rss_mib'),('cpu_log_ratio','api_cpu_us_per_response')]}})
        assert len(blocks)==8
        out['comparisons'][pair]={'variants':variants,'rps':estimate([b['rps_log_ratio'] for b in blocks]),'rss':estimate([b['rss_log_ratio'] for b in blocks]),'api_cpu_us_per_response':estimate([b['cpu_log_ratio'] for b in blocks]),'sides':{side:{'variant':variants[side],**{key:{'geomean':statistics.geometric_mean(p[key] for p in points if p['side']==side),'median':statistics.median(p[key] for p in points if p['side']==side),'min':min(p[key] for p in points if p['side']==side),'max':max(p[key] for p in points if p['side']==side)} for key in ('rps','rss_mib')},'mean_cpu_cores':{n:statistics.mean(p['cpu_cores'][n] for p in points if p['side']==side) for n in points[0]['cpu_cores']},'mean_api_cpu_us_per_response':statistics.mean(p['api_cpu_us_per_response'] for p in points if p['side']==side),'max_throttled_share':{n:max(p['throttled_share_of_window'][n] for p in points if p['side']==side) for n in points[0]['throttled_usec']}} for side in 'AB'},'blocks':blocks,'points':points}
    # Static controls: generator ceiling reference (mean of static-pre-0/1 and static-0/1).
    static=[]
    for name in ('static-pre-0','static-pre-1','static-0','static-1'):
        if (E/name/'result.json').exists():
            r=read(E/name/'result.json')['result']
            samples=[json.loads(line) for line in (E/name/'telemetry.jsonl').read_text().splitlines()]
            m=[x for x in samples if r['measurement_start_unix']<=x['unix']<r['measurement_end_unix']]
            a,z=m[0],m[-1];gen=(z['targets']['gen']['cpu']['usage_usec']-a['targets']['gen']['cpu']['usage_usec'])/1e6/(z['monotonic']-a['monotonic'])
            api=(z['targets']['api']['cpu']['usage_usec']-a['targets']['api']['cpu']['usage_usec'])/1e6/(z['monotonic']-a['monotonic'])
            static.append({'point':name,'rps':r['goodput_rps'],'gen_cpu':gen,'gen_us_per_response':gen*1e6/r['goodput_rps'],'api_us_per_response':api*1e6/r['goodput_rps'],'cpu_shares':cpu_shares(a,z)})
    out['static_controls']=static
    ref={'rps':statistics.mean(x['rps'] for x in static),'gen_cpu':statistics.mean(x['gen_cpu'] for x in static)} if static else None
    out['static_reference']=ref
    out['static_controls_complete']=len(static)==4
    pre=[x for x in static if x['point'].startswith('static-pre')];post=[x for x in static if not x['point'].startswith('static-pre')]
    if pre and post:
        drift={k:100*(statistics.mean(x[k] for x in post)/statistics.mean(x[k] for x in pre)-1) for k in ('rps','gen_us_per_response','api_us_per_response')}
        # API cost of the static server is ~20 us/response, too small for a stable percentage.
        out['host_drift']={'post_vs_pre_percent':drift,'flag':any(abs(drift[k])>10 for k in ('rps','gen_us_per_response'))}
    else:
        out['host_drift']={'post_vs_pre_percent':None,'flag':None,'reason':'static controls before and after main are both required'}
    # In-series drift trace: fastapi-msgspec appears in six pairs in every round.
    trace={}
    for pair in out['comparisons'].values():
        for p in pair['points']:
            if p['variant']==CONTROL:trace.setdefault(p['round'],{})[p['path']]=p
    out['control_drift_trace']={'variant':CONTROL,'rounds':[{'round':r,'mean_rps':statistics.mean(p['rps'] for p in v.values()),'mean_api_cpu_us_per_response':statistics.mean(p['api_cpu_us_per_response'] for p in v.values()),'points':len(v)} for r,v in sorted(trace.items())]}
    # Per-variant classification over all main points of the variant.
    by_variant={}
    for pair in out['comparisons'].values():
        for p in pair['points']:by_variant.setdefault(p['variant'],{})[p['path']]=p
    out['variants']={}
    for v,pts in by_variant.items():
        pts=list(pts.values())
        api=statistics.mean(p['cpu_cores']['api'] for p in pts);gen=statistics.mean(p['cpu_cores']['gen'] for p in pts);rps=statistics.mean(p['rps'] for p in pts)
        out['variants'][v]={'points':len(pts),'mean_rps':rps,'mean_api_cpu':api,'mean_gen_cpu':gen,
          'mean_api_cpu_us_per_response':statistics.mean(p['api_cpu_us_per_response'] for p in pts),
          'mean_gen_cpu_us_per_response':statistics.mean(p['cpu_cores']['gen']*1e6/p['rps'] for p in pts),
          'rule_lower_bound':api<0.95,'rule_redis_limited':max(p['throttled_share_of_window'].get('redis',0) for p in pts)>0.01,
          'rps_share_of_static':rps/ref['rps'] if ref else None,'gen_share_of_static':gen/ref['gen_cpu'] if ref else None}
    for name,pair in out['comparisons'].items():
        for side in pair['sides'].values():
            vv=out['variants'][side['variant']]
            side['rule_lower_bound']=vv['rule_lower_bound'];side['rule_redis_limited']=vv['rule_redis_limited']
            side['mean_gen_cpu_us_per_response']=statistics.mean(p['cpu_cores']['gen']*1e6/p['rps'] for p in pair['points'] if p['variant']==side['variant'])
        sides=[out['variants'][pair['variants'][x]] for x in 'AB']
        pair['rule_direction_not_server_result']=(ref is None) or any(x['gen_share_of_static']>=0.9 for x in sides)
        # Claim decision per pair, fixed by the protocol (pair_ratio, manipulation_check).
        excludes0=lambda e:e['nominal_95pct_t_interval'][0]>0 or e['nominal_95pct_t_interval'][1]<0
        lower=any(x['rule_lower_bound'] for x in sides)
        pair['primary_metric']='api_cpu_us_per_response' if pair['rule_direction_not_server_result'] or name=='fastapi-msgpack-cache' else 'rps'
        pair['primary_direction_claim_allowed']=excludes0(pair[pair['primary_metric']])
        pair['rps_direction_claim_allowed']=excludes0(pair['rps']) and not pair['rule_direction_not_server_result'] and (name!='fastapi-msgpack-cache' or pair['api_cpu_us_per_response']['nominal_95pct_t_interval'][1]<0)
        pair['cpu_compared_at_different_loads']=lower
        pair['cpu_interval_mirrors_rps']=not lower
    m=out['comparisons'].get('fastapi-msgpack-cache')
    if m:
        a,b=(m['sides'][x]['mean_api_cpu_us_per_response'] for x in 'AB')
        ci=m['api_cpu_us_per_response']['nominal_95pct_t_interval']
        out['manipulation_check']={'A_us_per_response':a,'B_us_per_response':b,'descriptive_mean_B_below_A':b<a,'cpu_interval_percent':ci,'outcome':'msgpack_cpu_advantage' if ci[1]<0 else 'msgpack_costlier' if ci[0]>0 else 'no_detectable_difference','msgpack_cpu_advantage_claim_allowed':ci[1]<0}
    # Equal-load open-loop points.
    eq={}
    for path in sorted(E.glob('matched-*')):
        if not (path/'result.json').exists():continue
        raw=read(path/'result.json');r=raw['result']
        samples=[json.loads(line) for line in (path/'telemetry.jsonl').read_text().splitlines()]
        mm=[x for x in samples if r['measurement_start_unix']<=x['unix']<r['measurement_end_unix']]
        a,z=mm[0],mm[-1];dt=z['monotonic']-a['monotonic']
        cpu={n:(z['targets'][n]['cpu']['usage_usec']-a['targets'][n]['cpu']['usage_usec'])/1e6/dt for n in ('api','gen')}
        eq.setdefault(raw['variant'],[]).append({'point':path.name,'goodput_rps':r['goodput_in_window_rps'],'offered_rps':r['offered_rps'],'api_cpu':cpu['api'],'api_cpu_us_per_response':cpu['api']*1e6/r['goodput_in_window_rps'],'p99_ms':r['successful_latency_ms']['p99'],'cache':r['cache'],'cpu_shares':cpu_shares(a,z)})
    out['equal_load']=eq
    # claim_scope: the cache-vs-optimizations statement needs all three conditions.
    c=out['comparisons'].get('cache-vs-optimizations')
    if c:
        ea,eb=(eq.get(c['variants'][x],[]) for x in 'AB')
        below=len(ea)==2 and len(eb)==2 and max(x['api_cpu_us_per_response'] for x in eb)<min(x['api_cpu_us_per_response'] for x in ea)
        rb=c['rps']['nominal_95pct_t_interval'][0]>0;nl=not c['rule_direction_not_server_result']
        out['claim_scope_cache_vs_optimizations']={'rps_interval_above_0_for_B':rb,'not_client_limited':nl,'equal_load_both_B_points_below_both_A_points':below,'statement_allowed':rb and nl and below}
    pts=[p for c in out['comparisons'].values() for p in c['points']]
    out['host']={'telemetry':host_summary(),'steal_flag_threshold_share':STEAL_FLAG,
      'max_point_steal_share':{c:max(p['cpu_shares'][c]['steal'] for p in pts) for c in ('cpu0','cpu1','cpu2','cpu3')},
      'max_point_softirq_share':{c:max(p['cpu_shares'][c]['softirq'] for p in pts) for c in ('cpu0','cpu1','cpu2','cpu3')},
      'points_with_steal_above_flag':[p['path'] for p in pts if any(p['cpu_shares'][c]['steal']>STEAL_FLAG for c in ('cpu0','cpu1','cpu2','cpu3'))],
      'static_and_equal_load_points_with_steal_above_flag':[x['point'] for x in static+[y for v in eq.values() for y in v] if any(x['cpu_shares'][c]['steal']>STEAL_FLAG for c in ('cpu0','cpu1','cpu2','cpu3'))]}
    out['complete']=True
    (E/'summary.json').write_text(json.dumps(out,indent=2))
    print(json.dumps({k:{key:v[key] for key in ('rps','rss','sides')} for k,v in out['comparisons'].items()},indent=2))
if __name__=='__main__':main()
