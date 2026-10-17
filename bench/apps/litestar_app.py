"""Narrow cities GET comparison; shared service mounted on PYTHONPATH.

Run with RESPONSE_PATH=msgspec and CACHE_MODE=bypass. The final Linux image
must install the frozen custom msgspec build before importing this module.
"""
from contextlib import asynccontextmanager
import os

import msgspec
from cashews import Cache
from litestar import Litestar, Request, Response, get

from directory import msgspec_schemas as s
from directory.dto import CityRow
from directory.presentation import output_preparer
from directory.repository import Repository
from directory.schema_ops import data
from directory.service import DirectoryService
from fastapi_extension_msgspec.msgspec_validation import _http_int

if os.environ.get('RESPONSE_PATH') != 'msgspec' or os.environ.get('CACHE_MODE') != 'bypass':
    raise RuntimeError('This comparison requires RESPONSE_PATH=msgspec and CACHE_MODE=bypass')

_output_encoder = msgspec.json.Encoder()
_output_decoder = msgspec.json.Decoder(s.Page[s.City])
_prepare = output_preparer(s.City, page=True)
_query_names = frozenset(s.CityQuery.__struct_fields__)


@asynccontextmanager
async def lifespan(app: Litestar):
    repository = Repository(os.environ['DATABASE_URL'])
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
    result, cache_state = await request.app.state.service.read(
        CityRow, data(query), prepare_output=_prepare,
    )
    # Match the strict MsgspecRouter boundary after the common presentation work.
    checked = _output_decoder.decode(_output_encoder.encode(result))
    return Response(checked, media_type='application/json', headers={'X-Cache': cache_state})


app = Litestar(route_handlers=[cities], lifespan=[lifespan], debug=False,
               openapi_config=None, logging_config=None)
