"""Cities GET on the full optimized stack, matching the FastAPI cache variant.

Same shared service as the FastAPI variant: direct asyncpg repository, no
output re-check after validation, Cashews view cache with a 60 s TTL and the
same key derivation. CACHE_CODEC selects how cached data is stored and
validated, exactly as in the FastAPI variant. Run under uvicorn --loop uvloop.
"""
from contextlib import asynccontextmanager
import hashlib
import json
import os

import msgspec
from cashews import Cache, CacheBackendInteractionError
from litestar import Litestar, Request, Response, get
from redis.exceptions import RedisError

from asyncpg_repository import Repository
from directory import msgspec_schemas as s
from directory.dto import CityRow
from directory.presentation import output_preparer
from directory.schema_ops import data, validate
from directory.service import DirectoryService
from fastapi_extension_msgspec.msgspec_validation import _http_int

_REQUIRED = {'RESPONSE_PATH': 'msgspec', 'OUTPUT_MODEL': 'none', 'DB_BACKEND': 'asyncpg',
             'CACHE_MODE': 'bypass', 'VIEW_CACHE': 'enabled'}
if any(os.environ.get(k) != v for k, v in _REQUIRED.items()):
    raise RuntimeError(f'This comparison requires {_REQUIRED}')

_prepare = output_preparer(s.City, page=True)
_query_names = frozenset(s.CityQuery.__struct_fields__)


@asynccontextmanager
async def lifespan(app: Litestar):
    repository = Repository(os.environ['DATABASE_URL'])
    # CACHE_CODEC=msgpack stores msgspec.msgpack.encode bytes. Cashews keeps bytes as
    # is with the null pickler, so no json.loads and no repr on read; the handler
    # decodes and validates them with msgspec.msgpack.decode(type=Page[City]).
    codec = os.environ.get('CACHE_CODEC', 'json')
    if codec not in {'json', 'msgpack'}:
        raise ValueError('CACHE_CODEC must be json or msgpack')
    cache = Cache(name='world-directory')
    if codec == 'msgpack':
        cache.setup(os.environ['REDIS_URL'], pickle_type='null', check_repr=False, suppress=False,
                    socket_timeout=2, socket_connect_timeout=2)
    else:
        cache.setup(os.environ['REDIS_URL'], pickle_type='json', suppress=False,
                    socket_timeout=2, socket_connect_timeout=2)
    service = DirectoryService(repository, cache, cache_enabled=False)

    @cache(ttl='60s', key='view:cities:{key}')
    async def cached_cities(key, query, state):
        result, _ = await service.read(CityRow, query, prepare_output=_prepare)
        state['cache'] = 'miss'
        return msgspec.msgpack.encode(result) if codec == 'msgpack' else data(result)

    app.state.service = service
    app.state.cache_codec = codec
    app.state.cached_cities = cached_cities
    try:
        yield
    finally:
        await service.close()


def _cache_error(request: Request, exc: Exception) -> Response:
    return Response({'error': 'cache_unavailable'}, status_code=503)


@get('/cities')
async def cities(request: Request) -> Response[s.Page[s.City]]:
    values = dict(request.query_params.multi_items())
    if values.keys() - _query_names:
        return Response({'error': 'invalid_fields'}, status_code=422)
    try:
        # Preserve the pinned FastAPI adapter's HTTP integer lexical contract.
        for name in ('limit', 'min_population'):
            if name in values:
                values[name] = _http_int(values[name])
        query = msgspec.convert(values, type=s.CityQuery, strict=False)
    except (msgspec.ValidationError, ValueError):
        return Response({'error': 'invalid_fields'}, status_code=422)
    values = data(query)
    key = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    state = {'cache': 'hit'}
    payload = await request.app.state.cached_cities(key, values, state)
    # Validate cached data before returning the public contract.
    if request.app.state.cache_codec == 'msgpack':
        page = msgspec.msgpack.decode(payload, type=s.Page[s.City])
    else:
        page = validate(s.Page[s.City], payload)
    return Response(page, media_type='application/json', headers={'X-Cache': state['cache']})


app = Litestar(route_handlers=[cities], lifespan=[lifespan], debug=False,
               openapi_config=None, logging_config=None,
               exception_handlers={CacheBackendInteractionError: _cache_error, RedisError: _cache_error})
