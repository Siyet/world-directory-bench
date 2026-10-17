import asyncio
import json

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from directory.api import app
from directory.models import Base, Version

COUNTRY = {'id': 'local:country:fixture', 'source': 'local', 'name': 'Test country', 'country_code': 'ZZ', 'continent': 'AN'}
DIVISION = {'id': 'local:division:fixture', 'source': 'local', 'name': 'Test region', 'country_id': COUNTRY['id'], 'code': 'ZZ.TEST', 'level': 1, 'parent_id': None}
CITY = {'id': 'local:city:fixture', 'source': 'local', 'name': 'Test city', 'country_id': COUNTRY['id'], 'division_id': DIVISION['id'], 'latitude': 1.0, 'longitude': 2.0, 'population': 42, 'feature_code': 'PPL'}
AIRPORT = {'id': 'local:airport:fixture', 'source': 'local', 'name': 'Test airport', 'country_id': COUNTRY['id'], 'region_id': DIVISION['id'], 'city_id': CITY['id'], 'latitude': 1.0, 'longitude': 2.0, 'airport_type': 'small_airport', 'scheduled_service': False, 'municipality': 'Test city', 'iata_code': None}
RECORDS = [('countries', COUNTRY), ('divisions', DIVISION), ('cities', CITY), ('airports', AIRPORT)]


@pytest_asyncio.fixture
async def client():
    async with app.router.lifespan_context(app):
        store = app.state.service
        async with store.repository.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text('TRUNCATE airports, cities, divisions, countries, dataset_version'))
            await connection.execute(text('INSERT INTO dataset_version VALUES (1, 1)'))
        # This Redis database is exclusive to the disposable compose project.
        await store.cache.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            yield client


async def seed(client):
    for resource, body in RECORDS:
        response = await client.post('/' + resource, json=body)
        assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_crud_all_resources_and_restrict_delete(client):
    await seed(client)
    for resource, body in RECORDS:
        path = f'/{resource}/{body["id"]}'
        assert (await client.get(path)).json() == body
        assert (await client.post('/' + resource, json=body)).status_code == 409
        updated = dict(body, name='Изменённое имя')
        response = await client.put(path, json=updated)
        assert response.status_code == 200, response.text
        assert response.json() == updated
        assert (await client.get(path)).json() == updated
    assert (await client.delete('/countries/' + COUNTRY['id'])).status_code == 409
    assert (await client.delete('/cities/' + CITY['id'])).status_code == 409
    for resource, body in reversed(RECORDS):
        response = await client.delete(f'/{resource}/{body["id"]}')
        assert response.status_code == 204
        assert response.content == b''
        assert (await client.get(f'/{resource}/{body["id"]}')).status_code == 404
        assert (await client.delete(f'/{resource}/{body["id"]}')).status_code == 404


@pytest.mark.asyncio
async def test_pagination_filters_and_literal_prefix(client):
    await seed(client)
    for suffix, name, pop in [('b', 'literal%_value', 10), ('c', 'literalXXvalue', 100), ('d', 'test lower', 200)]:
        response = await client.post('/cities', json=dict(CITY, id='local:city:' + suffix, name=name, population=pop))
        assert response.status_code == 201
    response = await client.get('/cities', params={'limit': 2})
    page = response.json()
    assert [x['id'] for x in page['items']] == ['local:city:b', 'local:city:c']
    next_page = (await client.get('/cities', params={'limit': 2, 'after_id': page['next_after_id']})).json()
    assert [x['id'] for x in next_page['items']] == ['local:city:d', CITY['id']]
    assert next_page['next_after_id'] is None
    assert (await client.get('/cities', params={'name_prefix': 'literal%_'})).json()['items'][0]['id'] == 'local:city:b'
    assert len((await client.get('/cities', params={'name_prefix': 'literal%_'})).json()['items']) == 1
    assert (await client.get('/cities', params={'name_prefix': "' OR 1=1 --"})).json()['items'] == []
    assert (await client.get('/cities', params={'name_prefix': 'Literal'})).json()['items'] == []
    results = (await client.get('/cities', params={'country_code': 'ZZ', 'division_id': DIVISION['id'], 'min_population': 100})).json()['items']
    assert {x['id'] for x in results} == {'local:city:c', 'local:city:d'}
    assert (await client.get('/airports', params={'scheduled_service': 'true'})).json()['items'] == []
    assert len((await client.get('/airports', params={'scheduled_service': 'false', 'airport_type': 'small_airport'})).json()['items']) == 1
    assert len((await client.get('/divisions', params={'source': 'local', 'level': 1})).json()['items']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['limit=0', 'limit=1001', 'unknown=1', 'country_code=zz', 'min_population=-1', 'name_prefix=%00'])
async def test_invalid_query(client, query):
    assert (await client.get('/cities?' + query)).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'population': True}, {'population': '42'}, {'population': -1}, {'population': 2**63}, {'latitude': 91}, {'longitude': -181}, {'name': ''}, {'name': None}, {'name': '\x00'}, {'name': '\ud800'}, {'extra': 1}, {'feature_code': 'PPLQ'}, {'id': 'ourairports:city:1'}])
async def test_strict_validation(client, change):
    response = await client.post('/cities', content=json.dumps(dict(CITY, **change)), headers={'Content-Type': 'application/json'})
    assert response.status_code == 422, response.text
    assert response.json() == {'error': 'invalid_fields'}


@pytest.mark.asyncio
async def test_json_contract(client):
    headers = {'Content-Type': 'application/json'}
    for body in [b'{', b'{"name":"\xff"}', b'{"extra":"\xff"}', b'{"x":[1,]}', b'[' * 33 + b'0' + b']' * 33]:
        response = await client.post('/countries', content=body, headers=headers)
        assert response.status_code == 400, response.text
    for body in [b'null', b'[]', b'1', b'"abc"']:
        assert (await client.post('/countries', content=body, headers=headers)).status_code == 422
    for value in [float('nan'), float('inf'), -float('inf')]:
        assert (await client.post('/cities', content=json.dumps(dict(CITY, latitude=value)), headers=headers)).status_code == 400
    missing = dict(CITY)
    del missing['division_id']
    assert (await client.post('/cities', json=missing)).status_code == 422
    # Duplicate keys are invalid even if escaped differently or later overwritten.
    body = json.dumps(COUNTRY)[:-1] + ', "name":"last"}'
    response = await client.post('/countries', content=body, headers=headers)
    assert response.status_code == 400
    for body in [b'{"name":1,"na\\u006de":2}', b'{"x":{"a":1,"a":2}}', json.dumps(COUNTRY).encode('utf-16le'), b'\xef\xbb\xbf{}']:
        assert (await client.post('/countries', content=body, headers=headers)).status_code == 400
    assert (await client.post('/countries', content='{}')).status_code == 415
    assert (await client.post('/countries', content='{}', headers={'Content-Type': 'application/json; charset=latin1'})).status_code == 415
    assert (await client.post('/countries', content='{}', headers={**headers, 'Content-Encoding': 'gzip'})).status_code == 415
    assert (await client.post('/countries', content=b' ' * 65537, headers=headers)).status_code == 413

    async def chunks():
        for _ in range(65):
            yield b' ' * 1024
    assert (await client.post('/countries', content=chunks(), headers=headers)).status_code == 413


@pytest.mark.asyncio
async def test_references_and_identity(client):
    await seed(client)
    other = dict(COUNTRY, id='local:country:other', country_code='AA')
    assert (await client.post('/countries', json=other)).status_code == 201
    wrong = dict(CITY, id='local:city:other', country_id=other['id'])
    assert (await client.post('/cities', json=wrong)).status_code == 409
    assert (await client.put('/divisions/' + DIVISION['id'], json=dict(DIVISION, country_id=other['id']))).status_code == 409
    assert (await client.put('/divisions/' + DIVISION['id'], json=dict(DIVISION, level=2))).status_code == 409
    assert (await client.put('/cities/' + CITY['id'], json=dict(CITY, id='local:city:other'))).status_code == 409
    assert (await client.post('/divisions', json=dict(DIVISION, id='local:division:child', code='ZZ.C', parent_id=DIVISION['id'], level=1))).status_code == 409
    assert (await client.post('/cities', json=dict(CITY, id='geonames:city:1', source='geonames'))).status_code == 409
    assert (await client.post('/airports', json=dict(AIRPORT, id='local:airport:null', region_id=None, city_id=None))).status_code == 201
    assert (await client.put('/cities/local:city:absent', json=CITY)).status_code == 404


@pytest.mark.asyncio
async def test_cache_invalidation_and_late_fill(client, monkeypatch):
    await seed(client)
    path = '/cities/' + CITY['id']
    started = asyncio.Event()
    release = asyncio.Event()
    store = app.state.service
    real_set = store.cache.set
    old_keys = []

    async def delayed_set(key, *args, **kwargs):
        old_keys.append(key)
        started.set()
        await release.wait()
        return await real_set(key, *args, **kwargs)

    monkeypatch.setattr(store.cache, 'set', delayed_set)
    old_read = asyncio.create_task(client.get(path))
    await asyncio.wait_for(started.wait(), 5)
    updated = dict(CITY, name='After committed mutation')
    assert (await client.put(path, json=updated)).status_code == 200
    release.set()
    assert (await old_read).json()['name'] == CITY['name']
    monkeypatch.setattr(store.cache, 'set', real_set)
    # A late fill did occur in actual Redis, under the previous generation.
    assert (await store.cache.get(old_keys[0]))['name'] == CITY['name']
    fresh = await client.get(path)
    assert fresh.headers['X-Cache'] == 'miss'
    assert fresh.json() == updated
    hot = await client.get(path)
    assert hot.headers['X-Cache'] == 'hit'
    assert hot.json() == updated
    await client.delete('/airports/' + AIRPORT['id'])
    await client.delete(path)
    assert (await client.get(path)).status_code == 404
    assert (await client.get('/cities')).json()['items'] == []


@pytest.mark.asyncio
async def test_cache_filters_and_concurrent_writes(client):
    await seed(client)
    for population, expected in [(0, 1), (100, 0), (0, 1), (100, 0)]:
        response = await client.get('/cities', params={'min_population': population})
        assert len(response.json()['items']) == expected
    store = app.state.service
    async with store.repository.sessions() as session:
        before = await session.scalar(select(Version.value))
    replies = await asyncio.gather(*(client.post('/cities', json=dict(CITY, id=f'local:city:parallel{i}')) for i in range(12)))
    assert [x.status_code for x in replies] == [201] * 12
    async with store.repository.sessions() as session:
        after = await session.scalar(select(Version.value))
    assert after == before + 12
    assert len((await client.get('/cities')).json()['items']) == 13


@pytest.mark.asyncio
async def test_openapi_and_baseline_output_contract(client):
    spec = (await client.get('/openapi.json')).json()
    assert len(spec['paths']) == 10
    assert spec['paths']['/cities/search'].keys() == {'post'}
    assert spec['paths']['/cities/validate'].keys() == {'post'}
    for resource, _ in RECORDS:
        assert {'get', 'post'} <= spec['paths']['/' + resource].keys()
        assert {'get', 'put', 'delete'} <= spec['paths']['/' + resource + '/{record_id}'].keys()
    await seed(client)
    app.state.service.cache_enabled = False
    response = await client.get('/cities/' + CITY['id'])
    assert response.headers['X-Cache'] == 'bypass'
    assert response.json() == CITY


@pytest.mark.asyncio
async def test_atomic_loader_rejects_nonempty_and_rolls_back(client, tmp_path):
    from directory.load import load

    for resource, body in RECORDS:
        data = dict(body)
        if resource == 'airports':
            data['city_id'] = 'local:city:missing'
        (tmp_path / (resource + '.jsonl')).write_text(json.dumps(data) + '\n')
    with pytest.raises(IntegrityError):
        await load(tmp_path)
    # Earlier files were inserted in the failed transaction and must all roll back.
    for resource, _ in RECORDS:
        assert (await client.get('/' + resource)).json()['items'] == []
    (tmp_path / 'airports.jsonl').write_text(json.dumps(AIRPORT) + '\n')
    result = await load(tmp_path)
    assert result['counts'] == {'countries': 1, 'divisions': 1, 'cities': 1, 'airports': 1}
    for resource, body in RECORDS:
        assert (await client.get('/' + resource)).json()['items'] == [body]
    with pytest.raises(ValueError, match='empty'):
        await load(tmp_path)
    assert (await client.get('/countries')).json()['items'] == [COUNTRY]
