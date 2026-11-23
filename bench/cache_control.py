import os,json
os.environ.update(RESPONSE_PATH='msgspec',OUTPUT_MODEL='none',DB_BACKEND='asyncpg',CACHE_MODE='bypass',VIEW_CACHE='enabled')
from fastapi.testclient import TestClient
from diagnostic_app import app
from unittest.mock import patch
from redis import Redis
redis=Redis.from_url(os.environ['REDIS_URL'])
redis.flushdb() # isolated benchmark Redis only
with TestClient(app, raise_server_exceptions=True) as c:
    first=c.get('/cities?limit=17')
    assert first.status_code==200 and first.headers['x-cache']=='miss'
    keys=list(redis.scan_iter())
    assert keys
    ttl={key.decode():redis.ttl(key) for key in keys}
    assert any(0 < value <= 60 for value in ttl.values()),ttl
    repo=app.state.service.repository
    with patch.object(app.state.service,'read',side_effect=AssertionError('service read on hit')),patch.object(repo,'transaction',side_effect=AssertionError('transaction on hit')),patch.object(type(repo._pool),'acquire',side_effect=AssertionError('connection checkout on hit')):
        hit=c.get('/cities?limit=17')
        assert hit.status_code==200 and hit.headers['x-cache']=='hit'
        assert hit.json()==first.json()
    for key in keys:redis.expire(key,0)
    refill=c.get('/cities?limit=17')
    assert refill.status_code==200 and refill.headers['x-cache']=='miss'
    assert refill.json()==first.json()
    invalid=c.get('/cities?limit=0')
    assert invalid.status_code==422
    print(json.dumps({'first':'miss','second':'hit','forced_expiry_refill':'miss','equal_bodies':True,'no_service_transaction_checkout_on_hit':True,'observed_ttl_seconds':ttl,'invalid_limit':422,'natural_expiry':'measured separately during60second HTTPwindows'}))
