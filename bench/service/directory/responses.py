"""Only the output path changes; input, DB, ORM and cache policy stay fixed."""
import os

from fastapi.responses import JSONResponse, ORJSONResponse, UJSONResponse
import orjson
from pydantic import BaseModel, TypeAdapter
from starlette.responses import Response

from fastapi_extension_msgspec.msgspec_responses import MsgspecJSONResponse

from .schema_ops import schemas as s

PATHS = ('explicit', 'inferred', 'json-response', 'ujson-response', 'orjson-response', 'pydantic-json-response', 'trusted-orjson-response', 'msgspec', 'msgspec-json-response')
RESPONSE_PATH = os.environ.get('RESPONSE_PATH', 'explicit')
if RESPONSE_PATH not in PATHS:
    raise ValueError(f'Unknown RESPONSE_PATH: {RESPONSE_PATH}')
TRUSTED = RESPONSE_PATH == 'trusted-orjson-response'
ADAPTERS = ({model: TypeAdapter(model) for record in (s.Country, s.Division, s.City, s.Airport) for model in (record, s.Page[record])}
            if RESPONSE_PATH == 'pydantic-json-response' else {})
RESPONSE_CLASSES = {'json-response': JSONResponse, 'ujson-response': UJSONResponse, 'orjson-response': ORJSONResponse}


def declaration(schema):
    # Inferred uses the real function return annotation. All other paths retain
    # explicit response_model for the same OpenAPI document, including Response instances.
    if os.environ.get('OUTPUT_MODEL') == 'none':
        return {'response_model': None}
    return {} if RESPONSE_PATH == 'inferred' else {'response_model': schema}


def render(result, *, status_code=200, headers=None):
    if RESPONSE_PATH in {'explicit', 'inferred', 'msgspec'}:
        return result
    if RESPONSE_PATH == 'msgspec-json-response':
        # This explicit Response uses presentation/input validation, bypassing Router output validation.
        return MsgspecJSONResponse(result, status_code=status_code, headers=headers)
    if RESPONSE_PATH == 'pydantic-json-response':
        # Presentation has validated every record, including cached values. dump_json
        # serializes to bytes and does not itself validate the value.
        return Response(ADAPTERS[type(result)].dump_json(result), status_code=status_code, headers=headers, media_type='application/json')
    if TRUSTED:
        # Mutations return an already validated input model; GETs return raw field maps.
        data = result.model_dump(mode='json') if isinstance(result, BaseModel) else result
        return Response(orjson.dumps(data), status_code=status_code, headers=headers, media_type='application/json')
    # This conversion is deliberately inside the measured request path.
    return RESPONSE_CLASSES[RESPONSE_PATH](result.model_dump(mode='json'), status_code=status_code, headers=headers)
