import json

import pytest

from test_contract import CITY, COUNTRY, DIVISION, client, seed  # noqa: F401

OTHER_COUNTRY = dict(COUNTRY, id='local:country:other', country_code='YY', name='Other')
CITIES = [
    dict(CITY, id='local:city:a', name='Alpha', population=10, feature_code='PPL'),
    dict(CITY, id='local:city:b', name='Beta', population=500, feature_code='PPLA'),
    dict(CITY, id='local:city:c', name='Bravo', population=5000, feature_code='PPLC', division_id=None),
    dict(CITY, id='local:city:d', name='Delta', population=50, country_id=OTHER_COUNTRY['id'], division_id=None),
]


async def search(client, body, **kwargs):
    return await client.post('/cities/search', content=json.dumps(body), headers={'Content-Type': 'application/json'}, **kwargs)


async def seed_search(client):
    await seed(client)
    assert (await client.post('/countries', json=OTHER_COUNTRY)).status_code == 201
    for body in CITIES:
        assert (await client.post('/cities', json=body)).status_code == 201


def ids(response):
    assert response.status_code == 200, response.text
    return [item['id'] for item in response.json()['items']]


@pytest.mark.asyncio
async def test_search_filters_order_and_pages(client):
    await seed_search(client)
    assert ids(await search(client, {})) == ['local:city:a', 'local:city:b', 'local:city:c', 'local:city:d', 'local:city:fixture']
    assert ids(await search(client, {'filter': {'country_codes': ['YY']}})) == ['local:city:d']
    assert ids(await search(client, {'filter': {'country_codes': ['ZZ', 'YY'], 'population': {'min': 50, 'max': 500}}})) == ['local:city:b', 'local:city:d']
    assert ids(await search(client, {'filter': {'feature_codes': ['PPLA', 'PPLC']}, 'order': '-id'})) == ['local:city:c', 'local:city:b']
    assert ids(await search(client, {'filter': {'name_prefix': 'B', 'division_ids': [DIVISION['id']]}})) == ['local:city:b']
    assert ids(await search(client, {'filter': {'sources': ['geonames']}})) == []
    first = await search(client, {'order': '-id', 'limit': 2})
    assert ids(first) == ['local:city:fixture', 'local:city:d'] and first.json()['next_after_id'] == 'local:city:d'
    second = await search(client, {'order': '-id', 'limit': 2, 'after_id': 'local:city:d'})
    assert ids(second) == ['local:city:c', 'local:city:b']
    assert (await search(client, {'filter': {'name_prefix': '%'}})).json()['items'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    {'unknown': 1},
    {'filter': {'unknown': 1}},
    {'limit': 0},
    {'limit': 1001},
    {'limit': '10'},
    {'limit': 10.0},
    {'order': 'name'},
    {'filter': {'country_codes': []}},
    {'filter': {'country_codes': ['ZZ'] * 51}},
    {'filter': {'country_codes': 'ZZ'}},
    {'filter': {'feature_codes': ['XXX']}},
    {'filter': {'population': {'min': 10, 'max': 1}}},
    {'filter': {'population': {'min': -1}}},
    {'filter': {'name_prefix': ''}},
    {'filter': {'name_prefix': 'a\x00'}},
    {'filter': None},
    [],
])
async def test_search_invalid_body(client, body):
    response = await search(client, body)
    assert response.status_code == 422, response.text
    assert response.json() == {'error': 'invalid_fields'}


@pytest.mark.asyncio
async def test_search_malformed_and_media_type(client):
    response = await client.post('/cities/search', content=b'{"limit":', headers={'Content-Type': 'application/json'})
    assert response.status_code == 400 and response.json() == {'error': 'invalid_json'}
    response = await client.post('/cities/search', content=b'{}', headers={'Content-Type': 'text/plain'})
    assert response.status_code == 415


async def ingest(client, body):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return await client.post('/cities/validate', content=raw, headers={'Content-Type': 'application/json'})


@pytest.mark.asyncio
async def test_ingest_validates_batch_without_writes(client):
    response = await ingest(client, {'items': CITIES})
    assert response.status_code == 200, response.text
    assert response.json() == {'accepted': len(CITIES)}
    # Nothing was written: the cities are not readable afterwards.
    assert (await client.get('/cities/' + CITIES[0]['id'])).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    {'items': []},
    {'items': [dict(CITY, population=-1)]},
    {'items': [dict(CITY, id='local:country:wrong')]},
    {'items': [dict(CITY, extra=1)]},
    {'items': [dict(CITY, latitude='1.0')]},
    {'items': [CITY], 'extra': 1},
    {'items': [dict(CITY, name='a\x00')]},
])
async def test_ingest_invalid_batch(client, body):
    response = await ingest(client, body)
    assert response.status_code == 422, response.text
    assert response.json() == {'error': 'invalid_fields'}


@pytest.mark.asyncio
async def test_ingest_malformed(client):
    response = await ingest(client, b'{"items":[')
    assert response.status_code == 400 and response.json() == {'error': 'invalid_json'}
