"""Contract checks for the common Cashews cache and the full msgspec routes."""
import json

from cashews import Cache
import httpx
import pytest
from pydantic import BaseModel, TypeAdapter
from sqlalchemy import event

from directory.api import app
from directory.responses import RESPONSE_PATH, TRUSTED
from test_contract import client, seed, RECORDS, CITY


@pytest.mark.asyncio
async def test_cache_contains_data_has_ttl_and_hit_keeps_generation_query(client):
    await seed(client)
    path = '/cities/' + CITY['id']
    miss = await client.get(path)
    assert miss.headers['X-Cache'] == 'miss'
    cache = app.state.service.cache
    entries = [(key, value) async for key, value in cache.get_match('wd:v1:*:cities:*')]
    assert len(entries) == 1
    key, cached = entries[0]
    assert type(cached) is dict and cached == CITY
    assert 0 < await cache.get_expire(key) <= 60
    raw = await cache.get_raw(key)
    assert json.loads(raw) == CITY
    statements = []
    def executed(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    engine = app.state.service.repository.engine.sync_engine
    event.listen(engine, 'before_cursor_execute', executed)
    try:
        hit = await client.get(path)
    finally:
        event.remove(engine, 'before_cursor_execute', executed)
    assert hit.headers['X-Cache'] == 'hit' and hit.json() == CITY
    assert any('SELECT dataset_version.value' in value for value in statements)
    assert not any('FROM cities' in value for value in statements)


@pytest.mark.asyncio
async def test_real_unavailable_cashews_backend_preserves_503(client, monkeypatch):
    await seed(client)
    broken = Cache(name='unavailable-project-test')
    broken.setup('redis://127.0.0.1:1/0', pickle_type='json', suppress=False,
                 socket_timeout=0.1, socket_connect_timeout=0.1)
    monkeypatch.setattr(app.state.service.cache, 'get', broken.get)
    try:
        response = await client.get('/cities/' + CITY['id'])
        assert response.status_code == 503
        assert response.json() == {'error': 'cache_unavailable'}
    finally:
        await broken.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    {'source': 'ourairports'}, {'name': '\x00'}, {'latitude': float('nan')},
    {'latitude': float('inf')}, {'population': 2**63},
])
async def test_semantically_invalid_cached_output_is_rejected(client, change):
    if TRUSTED:
        pytest.skip('trusted control intentionally has a different output contract')
    await seed(client)
    path = '/cities/' + CITY['id']
    await client.get(path)
    cache = app.state.service.cache
    entries = [(key, value) async for key, value in cache.get_match('wd:v1:*:cities:*')]
    await cache.set(entries[0][0], dict(CITY, **change), expire=60)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test') as safe:
        response = await safe.get(path)
    assert response.status_code == 500
    assert response.content == b'Internal Server Error'


@pytest.mark.asyncio
async def test_invalid_body_syntax_has_priority_over_field_errors(client):
    for body in [b'{', b'{"name":42,', b'{"unexpected":1,', b'{"population":true,', b'{"x":[1,]}', b'{"name":"\\ud800",', b'{"name":"\\udc00"']:
        response = await client.post('/cities', content=body, headers={'content-type': 'application/json'})
        assert response.status_code == 400, response.text
        assert response.json() == {'error': 'invalid_json'}


@pytest.mark.asyncio
async def test_msgspec_all_twenty_routes_have_no_pydantic_runtime(client, monkeypatch):
    if RESPONSE_PATH not in {'msgspec', 'msgspec-json-response'}:
        pytest.skip('runtime exclusion applies to the msgspec mode')
    def forbidden(*args, **kwargs):
        raise AssertionError('Pydantic runtime used on supported msgspec CRUD route')
    for name in ('validate_python', 'dump_json', 'dump_python'):
        monkeypatch.setattr(TypeAdapter, name, forbidden)
    for name in ('model_validate', 'model_validate_json', 'model_dump', 'model_dump_json'):
        monkeypatch.setattr(BaseModel, name, forbidden)
    await seed(client)
    for resource, body in RECORDS:
        path = f'/{resource}/{body["id"]}'
        for _ in range(2):
            response = await client.get(path)
            assert response.status_code == 200 and response.json() == body
            response = await client.get('/' + resource, params={'limit': 1, 'country_code': 'ZZ'})
            assert response.status_code == 200 and response.json()['items'] == [body]
        changed = dict(body, name='Runtime contract')
        response = await client.put(path, json=changed)
        assert response.status_code == 200 and response.json() == changed
    assert (await client.get('/cities?limit=0')).status_code == 422
    search = {'filter': {'country_codes': ['ZZ'], 'population': {'min': 1}}, 'limit': 1}
    for _ in range(2):
        response = await client.post('/cities/search', json=search)
        assert response.status_code == 200 and response.json()['items'][0]['id'] == CITY['id']
    assert (await client.post('/cities/search', json={'limit': 0})).status_code == 422
    for resource, body in reversed(RECORDS):
        response = await client.delete(f'/{resource}/{body["id"]}')
        assert response.status_code == 204 and response.content == b''


INTEGER_WIRE_CASES = [
    '+1', '-1', ' 1 ', '\t1\n', '01', '001', '1.0', '01.00', '+01.00',
    '-00.0', '1.000', '1.01', '1e0', '1E+0', '1e1', '1e-0', '1_0',
    '1_000', '01_00', '1.0_0', '1_.0', '_1', '1_', '1__0', '+ 1', '--1',
    '0x1', '0b1', '１', '١', '१', '\u00a01\u00a0', '\u20031\u2003',
    '\u00851\u0085', '\u200b1', '1.', ' .1', '1,0', '+', '-', '', '  ',
    'NaN', 'Inf', 'Infinity', '1.0000000000000001', '1e300', '1.0e0',
    '0001.000', '+0001.000', '1\x00', '0__1', '0___0', '0_', '0__',
    '0_1', '01__0', '0-1', '00-1', '-0-1', '+0-1', '0_1.0', '0_.0',
    '1_2.0', '0__1.00', '1\x1c', '\x1c1', '-00_1', '000.0',
    '1_0_0.00', '00.00', '00_.00', '0__1e0', '9223372036854775807',
    '9223372036854775808', '9' * 4301, '0' * 5000,
]


def test_vendored_integer_wire_grammar_matches_pinned_pydantic_control():
    from fastapi_extension_msgspec.msgspec_validation import _parameter_validator
    from typing import Annotated
    import msgspec
    target = Annotated[int, msgspec.Meta(ge=0, le=2**63 - 1)]
    check = _parameter_validator(target, False)
    control = TypeAdapter(int)
    for raw in INTEGER_WIRE_CASES:
        try:
            expected = control.validate_python(raw)
            allowed = 0 <= expected <= 2**63 - 1
        except ValueError:
            allowed = False
        result, errors = check(raw, loc=('query', 'integer'))
        assert (not errors) == allowed, repr(raw)
        if allowed:
            assert result == expected and type(result) is int
    strict = _parameter_validator(target, True)
    assert strict(1)[0] == 1
    for raw in INTEGER_WIRE_CASES:
        assert strict(raw)[1], repr(raw)
    # The same compiled normalization must apply inside nullable and list targets.
    sequence = _parameter_validator(list[target | None], False)
    result, errors = sequence(['+01.00', '1_0', None])
    assert not errors and result == [1, 10, None]


@pytest.mark.asyncio
async def test_integer_query_wire_forms_preserve_full_service_contract(client):
    await seed(client)
    control = TypeAdapter(int)
    for path, field, lower, upper in [('/cities', 'limit', 1, 1000),
                                      ('/cities', 'min_population', 0, 2**63 - 1),
                                      ('/divisions', 'level', 1, 5)]:
        for raw in INTEGER_WIRE_CASES:
            try:
                expected = control.validate_python(raw)
                allowed = lower <= expected <= upper
            except ValueError:
                allowed = False
            response = await client.get(path, params={field: raw})
            assert response.status_code == (200 if allowed else 422), (field, repr(raw), response.text)
            if allowed:
                count = (int(expected <= CITY['population']) if field == 'min_population'
                         else int(expected == 1) if field == 'level' else 1)
                assert len(response.json()['items']) == count
            else:
                assert response.json() == {'error': 'invalid_fields'}
