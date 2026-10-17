import json
import os
from pathlib import Path
import sys
from collections import Counter
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from directory.api import app
from directory.responses import RESPONSE_PATH, TRUSTED
from test_contract import client, seed, CITY


@pytest.mark.asyncio
async def test_invalid_database_output_never_leaks_in_validating_paths(client):
    await seed(client)
    store = app.state.service
    store.cache_enabled = False
    marker = 'BADTYPE'
    async with store.repository.engine.begin() as connection:
        await connection.execute(text('UPDATE cities SET feature_code = :value WHERE id = :id'), {'value': marker, 'id': CITY['id']})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test') as safe_client:
        for path in ['/cities/' + CITY['id'], '/cities']:
            response = await safe_client.get(path)
            if TRUSTED:
                assert response.status_code == 200
                assert marker in response.text
            else:
                assert response.status_code == 500
                assert marker not in response.text
                assert response.content == b'Internal Server Error'


@pytest.mark.asyncio
async def test_invalid_cached_output_never_leaks_in_validating_paths(client):
    await seed(client)
    path = '/cities/' + CITY['id']
    assert (await client.get(path)).status_code == 200
    store = app.state.service
    keys = [key async for key, value in store.cache.get_match('wd:v1:*:cities:*')]
    assert len(keys) == 1
    corrupted = dict(CITY, population=True, unexpected='CACHE-LEAK-MARKER')
    await store.cache.set(keys[0], corrupted, expire=60)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test') as safe_client:
        response = await safe_client.get(path)
        if TRUSTED:
            assert response.status_code == 200
            assert response.json() == corrupted
        else:
            assert response.status_code == 500
            assert b'CACHE-LEAK-MARKER' not in response.content
            assert response.content == b'Internal Server Error'


@pytest.mark.asyncio
async def test_serialization_edges_and_explicit_field_selection(client):
    await seed(client)
    body = dict(CITY, name='\u2028😀 slash/ quote" backslash\\ café 日本', population=9223372036854775807, latitude=-0.0)
    path = '/cities/' + CITY['id']
    changed = await client.put(path, json=body)
    assert changed.status_code == 200
    assert changed.json() == body
    for _ in range(2):
        response = await client.get(path)
        assert response.json() == body
        assert response.headers['content-type'] == 'application/json'
        assert set(response.json()) == set(CITY)
    assert (await client.get('/cities')).json()['items'] == [body]
    declared = (await client.get('/openapi.json')).json()
    assert declared['paths']['/cities']['post']['responses']['201']['content']['application/json']['schema']['$ref'].endswith('/City')
    assert declared['paths']['/cities/{record_id}']['get']['responses']['200']['content']['application/json']['schema']['$ref'].endswith('/City')
    if os.environ.get('EVIDENCE_DIR'):
        Path(os.environ['EVIDENCE_DIR'], f'openapi-{RESPONSE_PATH}.json').write_text(json.dumps(declared, sort_keys=True, indent=2) + '\n')


@pytest.mark.asyncio
async def test_response_trace_outside_timed_work(client):
    await seed(client)
    app.state.service.cache_enabled = False
    traces = {}
    for label, path in [('detail', '/cities/' + CITY['id']), ('list', '/cities?limit=100')]:
        counts = Counter()
        def profile(frame, event, arg):
            if event == 'call':
                module = frame.f_globals.get('__name__', '')
                name = frame.f_code.co_name
                qualified = frame.f_code.co_qualname
                if (module in {'directory.schemas', 'directory.msgspec_schemas'} and name in {'namespace', 'unicode_scalar_text', '__post_init__'}) or (
                    module.startswith('pydantic') and name in {'model_validate', 'model_dump', 'model_dump_json', 'validate_python', 'dump_json', 'dump_python'}
                ) or (module.startswith('fastapi') and name in {'serialize_response', 'validate', 'serialize', 'serialize_json', 'render'}) or (module == 'starlette.responses' and name == 'render'):
                    counts[module + '.' + qualified] += 1
                    if module.startswith('fastapi') and name == 'validate':
                        field = frame.f_locals.get('self')
                        counts[module + '.' + qualified + '[field=' + str(getattr(field, 'name', '?')) + ']'] += 1
            elif event == 'c_call' and getattr(arg, '__module__', '') in {'orjson', 'ujson'}:
                counts[getattr(arg, '__module__') + '.' + getattr(arg, '__name__', '?')] += 1
        sys.setprofile(profile)
        try:
            response = await client.get(path)
        finally:
            sys.setprofile(None)
        assert response.status_code == 200
        traces[label] = dict(sorted(counts.items()))
    evidence = os.environ.get('EVIDENCE_DIR')
    if evidence:
        Path(evidence).mkdir(parents=True, exist_ok=True)
        Path(evidence, f'trace-{RESPONSE_PATH}.json').write_text(json.dumps({'response_path': RESPONSE_PATH, 'cache': 'bypass', 'measurement': False, 'traces': traces}, indent=2) + '\n')
    for counts in traces.values():
        namespace_checks = counts.get('directory.schemas.Record.namespace', 0)
        if RESPONSE_PATH in {'msgspec', 'msgspec-json-response'}:
            assert counts.get('directory.msgspec_schemas.Record.__post_init__', 0) > 0
            assert not any(name.startswith('pydantic') for name in counts)
        else:
            assert (namespace_checks == 0) if TRUSTED else (namespace_checks > 0)
        fastapi_output = counts.get('fastapi.routing.serialize_response', 0)
        assert (fastapi_output > 0) if RESPONSE_PATH in {'explicit', 'inferred'} else (fastapi_output == 0)


@pytest.mark.asyncio
async def test_openapi_preserves_exact_population_limits_over_http(client):
    from directory.schemas import City, CityQuery

    response = await client.get('/openapi.json')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/json'
    spec = json.loads(response.content, parse_float=Decimal)
    population = spec['components']['schemas']['City']['properties']['population']
    original = City.model_json_schema()['properties']['population']
    assert original['maximum'] == 2**63 - 1
    assert type(original['maximum']) is int
    assert population['maximum'] == original['maximum']
    assert type(population['maximum']) is int
    assert population['minimum'] == original['minimum'] == 0
    query = next(parameter for parameter in spec['paths']['/cities']['get']['parameters'] if parameter['name'] == 'min_population' and parameter['in'] == 'query')
    query_integer = next(part for part in query['schema']['anyOf'] if part['type'] == 'integer')
    source_integer = next(part for part in CityQuery.model_json_schema()['properties']['min_population']['anyOf'] if part['type'] == 'integer')
    assert type(query_integer['maximum']) is int
    assert query_integer['maximum'] == source_integer['maximum'] == 2**63 - 1
    assert query_integer['minimum'] == source_integer['minimum'] == 0
    assert (await client.get('/openapi.json')).content == response.content


@pytest.mark.asyncio
async def test_population_and_filter_runtime_int64_boundaries(client):
    await seed(client)
    path = '/cities/' + CITY['id']
    for value in [0, 2**63 - 1]:
        body = dict(CITY, population=value)
        response = await client.put(path, json=body)
        assert response.status_code == 200
        assert response.json()['population'] == value
        assert type(response.json()['population']) is int
        assert (await client.get(path)).json() == body
        result = await client.get('/cities', params={'min_population': str(value)})
        assert result.status_code == 200
        assert result.json()['items'] == [body]
    for value in [-1, 2**63]:
        assert (await client.put(path, json=dict(CITY, population=value))).status_code == 422
        response = await client.get('/cities', params={'min_population': str(value)})
        assert response.status_code == 422
        assert response.json() == {'error': 'invalid_fields'}
    assert (await client.get(path)).json()['population'] == 2**63 - 1
