from contextlib import asynccontextmanager
import os
import hashlib
import json
from typing import Annotated

import msgspec

from fastapi import FastAPI, Query, Request, Response, Depends
from fastapi.exceptions import RequestValidationError
from redis.exceptions import RedisError
from cashews import Cache, CacheBackendInteractionError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from .schema_ops import schemas as s, MSGSPEC, data, validate
from .schemas import Error
from .repository import Repository
from .service import DirectoryService
from .presentation import ROW_TYPES, input_row, output_preparer
from .errors import Conflict, NotFound
from .responses import declaration, render
from .transport import BodyLimits


@asynccontextmanager
async def lifespan(app):
    mode = os.environ.get('CACHE_MODE', 'enabled')
    if mode not in {'enabled', 'bypass'}:
        raise ValueError('CACHE_MODE must be enabled or bypass')
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
    app.state.service = DirectoryService(repository, cache, cache_enabled=mode == 'enabled')
    app.state.cache_codec = codec
    if os.environ.get('VIEW_CACHE') == 'enabled':
        # The Pydantic path supports only the JSON codec (standard FastAPI plus a view cache).
        if mode != 'bypass' or (not MSGSPEC and codec != 'json'):
            raise ValueError('View-cache experiment requires downstream cache bypass; Pydantic only with the json codec')

        @cache(ttl='60s', key='view:cities:{key}')
        async def cached_cities(key, query, response):
            result, _ = await app.state.service.read(
                ROW_TYPES[s.City], query,
                prepare_output=output_preparer(s.City, page=True),
            )
            response.headers['X-Cache'] = 'miss'
            return msgspec.msgpack.encode(result) if codec == 'msgpack' else data(result)

        app.state.cached_cities = cached_cities
    try:
        yield
    finally:
        await app.state.service.close()


app = FastAPI(
    title='World Directory service comparison', version='0.1.0', lifespan=lifespan,
    responses={status: {'model': Error} for status in [400, 404, 409, 413, 415, 422, 503]},
)
app.add_middleware(BodyLimits)

_fastapi_openapi = app.openapi


def openapi():
    if app.openapi_schema is None:
        document = _fastapi_openapi()
        # Preserve the exact int64 bound after FastAPI's OpenAPI float coercion.
        if not MSGSPEC:
            document['components']['schemas']['City']['properties']['population']['maximum'] = (
                s.City.model_json_schema()['properties']['population']['maximum']
            )
    return app.openapi_schema


app.openapi = openapi


@app.exception_handler(NotFound)
async def not_found(request, exc):
    return JSONResponse({'error': 'not_found'}, status_code=404)


@app.exception_handler(Conflict)
async def conflict(request, exc):
    return JSONResponse({'error': exc.reason}, status_code=409)


@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc):
    detail = 'invalid_json' if exc.status_code == 400 else exc.detail
    return JSONResponse({'error': detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    syntax = any(error['type'] == 'json_invalid' for error in exc.errors())
    return JSONResponse({'error': 'invalid_json' if syntax else 'invalid_fields'}, status_code=400 if syntax else 422)


@app.exception_handler(CacheBackendInteractionError)
@app.exception_handler(RedisError)
async def cache_error(request, exc):
    return JSONResponse({'error': 'cache_unavailable'}, status_code=503)


async def read(request, response, schema, query=None, record_id=None, cacheable=True):
    if cacheable and schema is s.City and query is not None and hasattr(request.app.state, 'cached_cities'):
        values = data(query)
        key = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        response.headers['X-Cache'] = 'hit'
        payload = await request.app.state.cached_cities(key, values, response)
        # Validate cached data before returning the public contract.
        if request.app.state.cache_codec == 'msgpack':
            return msgspec.msgpack.decode(payload, type=s.Page[s.City])
        if not MSGSPEC:
            # Strict, like msgspec.convert(strict=True) on the msgspec path.
            return s.Page[s.City].model_validate(payload, strict=True)
        return validate(s.Page[s.City], payload)
    result, cache_state = await request.app.state.service.read(
        ROW_TYPES[schema], data(query) if query is not None else None, record_id,
        prepare_output=output_preparer(schema, page=query is not None),
    )
    response.headers['X-Cache'] = cache_state
    return render(result, headers={'X-Cache': cache_state})


async def save(request, body, replace_id=None):
    row = await request.app.state.service.save(input_row(body), replace_id)
    return output_preparer(type(body))(row, False).value


if MSGSPEC:
    from fastapi_extension_msgspec import MsgspecRouter, install_openapi
    from . import msgspec_queries as queries
    router = MsgspecRouter()
else:
    router = app


@router.get('/countries', **declaration(s.Page[s.Country]))
async def countries(request: Request, response: Response, query: Annotated[s.CountryQuery, Depends(queries.country_query) if MSGSPEC else Query()]) -> s.Page[s.Country]:
    return await read(request, response, s.Country, query)


@router.get('/countries/{record_id}', **declaration(s.Country))
async def country(request: Request, response: Response, record_id: s.Identifier) -> s.Country:
    return await read(request, response, s.Country, record_id=record_id)


@router.post('/countries', status_code=201, **declaration(s.Country))
async def create_country(request: Request, body: s.Country) -> s.Country:
    return render(await save(request, body), status_code=201)


@router.put('/countries/{record_id}', **declaration(s.Country))
async def replace_country(request: Request, record_id: s.Identifier, body: s.Country) -> s.Country:
    return render(await save(request, body, record_id))


@router.delete('/countries/{record_id}', status_code=204)
async def delete_country(request: Request, record_id: s.Identifier):
    await request.app.state.service.delete(ROW_TYPES[s.Country], record_id)
    return Response(status_code=204)


@router.get('/divisions', **declaration(s.Page[s.Division]))
async def divisions(request: Request, response: Response, query: Annotated[s.DivisionQuery, Depends(queries.division_query) if MSGSPEC else Query()]) -> s.Page[s.Division]:
    return await read(request, response, s.Division, query)


@router.get('/divisions/{record_id}', **declaration(s.Division))
async def division(request: Request, response: Response, record_id: s.Identifier) -> s.Division:
    return await read(request, response, s.Division, record_id=record_id)


@router.post('/divisions', status_code=201, **declaration(s.Division))
async def create_division(request: Request, body: s.Division) -> s.Division:
    return render(await save(request, body), status_code=201)


@router.put('/divisions/{record_id}', **declaration(s.Division))
async def replace_division(request: Request, record_id: s.Identifier, body: s.Division) -> s.Division:
    return render(await save(request, body, record_id))


@router.delete('/divisions/{record_id}', status_code=204)
async def delete_division(request: Request, record_id: s.Identifier):
    await request.app.state.service.delete(ROW_TYPES[s.Division], record_id)
    return Response(status_code=204)


@router.get('/cities', **declaration(s.Page[s.City]))
async def cities(request: Request, response: Response, query: Annotated[s.CityQuery, Depends(queries.city_query) if MSGSPEC else Query()]) -> s.Page[s.City]:
    return await read(request, response, s.City, query)


@router.post('/cities/search', **declaration(s.Page[s.City]))
async def search_cities(request: Request, response: Response, body: s.CitySearch) -> s.Page[s.City]:
    # Read-only search: the JSON body is the input contract. Never served from the view cache.
    return await read(request, response, s.City, body, cacheable=False)


@router.post('/cities/validate', **declaration(s.Accepted))
async def validate_cities(body: s.CityBatch) -> s.Accepted:
    # Input contract only: the batch is decoded and validated, nothing is written.
    return render(s.Accepted(accepted=len(body.items)))


@router.get('/cities/{record_id}', **declaration(s.City))
async def city(request: Request, response: Response, record_id: s.Identifier) -> s.City:
    return await read(request, response, s.City, record_id=record_id)


@router.post('/cities', status_code=201, **declaration(s.City))
async def create_city(request: Request, body: s.City) -> s.City:
    return render(await save(request, body), status_code=201)


@router.put('/cities/{record_id}', **declaration(s.City))
async def replace_city(request: Request, record_id: s.Identifier, body: s.City) -> s.City:
    return render(await save(request, body, record_id))


@router.delete('/cities/{record_id}', status_code=204)
async def delete_city(request: Request, record_id: s.Identifier):
    await request.app.state.service.delete(ROW_TYPES[s.City], record_id)
    return Response(status_code=204)


@router.get('/airports', **declaration(s.Page[s.Airport]))
async def airports(request: Request, response: Response, query: Annotated[s.AirportQuery, Depends(queries.airport_query) if MSGSPEC else Query()]) -> s.Page[s.Airport]:
    return await read(request, response, s.Airport, query)


@router.get('/airports/{record_id}', **declaration(s.Airport))
async def airport(request: Request, response: Response, record_id: s.Identifier) -> s.Airport:
    return await read(request, response, s.Airport, record_id=record_id)


@router.post('/airports', status_code=201, **declaration(s.Airport))
async def create_airport(request: Request, body: s.Airport) -> s.Airport:
    return render(await save(request, body), status_code=201)


@router.put('/airports/{record_id}', **declaration(s.Airport))
async def replace_airport(request: Request, record_id: s.Identifier, body: s.Airport) -> s.Airport:
    return render(await save(request, body, record_id))


@router.delete('/airports/{record_id}', status_code=204)
async def delete_airport(request: Request, record_id: s.Identifier):
    await request.app.state.service.delete(ROW_TYPES[s.Airport], record_id)
    return Response(status_code=204)


if MSGSPEC:
    app.include_router(router)
    install_openapi(app, router)
