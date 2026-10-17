"""Cities GET with Litestar, matching the FastAPI msgspec + asyncpg + uvloop variant.

Same shared service as the FastAPI variant: direct asyncpg repository, msgspec
contract prepared from the DB DTO, no output re-check, no cache (every request
reads PostgreSQL). Litestar encodes the prepared Struct page. Run under
uvicorn --loop uvloop.
"""
from contextlib import asynccontextmanager
import os

import msgspec
from cashews import Cache
from litestar import Litestar, Request, Response, get

from asyncpg_repository import Repository
from directory import msgspec_schemas as s
from directory.dto import CityRow
from directory.presentation import output_preparer
from directory.schema_ops import data
from directory.service import DirectoryService
from fastapi_extension_msgspec.msgspec_validation import _http_int

_REQUIRED = {'RESPONSE_PATH': 'msgspec', 'OUTPUT_MODEL': 'none', 'DB_BACKEND': 'asyncpg',
             'CACHE_MODE': 'bypass', 'VIEW_CACHE': 'disabled'}
if any(os.environ.get(k) != v for k, v in _REQUIRED.items()):
    raise RuntimeError(f'This comparison requires {_REQUIRED}')

_prepare = output_preparer(s.City, page=True)
_query_names = frozenset(s.CityQuery.__struct_fields__)


@asynccontextmanager
async def lifespan(app: Litestar):
    repository = Repository(os.environ['DATABASE_URL'])
    # Same service construction as the FastAPI variant with the cache bypassed.
    cache = Cache(name='world-directory')
    cache.setup(os.environ['REDIS_URL'], pickle_type='json', suppress=False,
                socket_timeout=2, socket_connect_timeout=2)
    app.state.service = DirectoryService(repository, cache, cache_enabled=False)
    try:
        yield
    finally:
        await app.state.service.close()


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
    page, cache_state = await request.app.state.service.read(CityRow, data(query), prepare_output=_prepare)
    return Response(page, media_type='application/json', headers={'X-Cache': cache_state})


app = Litestar(route_handlers=[cities], lifespan=[lifespan], debug=False,
               openapi_config=None, logging_config=None)
