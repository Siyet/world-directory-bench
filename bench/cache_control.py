import os,json
os.environ.update(RESPONSE_PATH='msgspec',OUTPUT_MODEL='none',DB_BACKEND='asyncpg',CACHE_MODE='bypass',VIEW_CACHE='enabled')
from fastapi.testclient import TestClient
from diagnostic_app import app
from unittest.mock import patch
with TestClient(app, raise_server_exceptions=True) as c:
    first=c.get('/cities?limit=17')
    assert first.status_code==200
    repo=app.state.service.repository
    with patch.object(app.state.service,'read',side_effect=AssertionError('service acquired on hit')),patch.object(repo,'transaction',side_effect=AssertionError('transaction on hit')),patch.object(type(repo._pool),'acquire',side_effect=AssertionError('connection checkout on hit')):
        hit=c.get('/cities?limit=17')
        assert hit.status_code==200 and hit.headers['x-cache']=='hit'
        assert hit.json()==first.json()
    invalid=c.get('/cities?limit=0')
    assert invalid.status_code==422
    print(json.dumps({'miss_status':first.status_code,'initial_cache':first.headers['x-cache'],'hit_status':hit.status_code,'hit_cache':hit.headers['x-cache'],'equal':hit.json()==first.json(),'no_service_transaction_checkout':True,'invalid_limit_status':invalid.status_code}))
