"""The effective response_model is the sole switch for msgspec output validation."""
from typing import Annotated, Generic, TypeVar

import msgspec
import pytest
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from fastapi_extension_msgspec import MsgspecRouter, install_openapi


class City(msgspec.Struct):
    population: Annotated[int, msgspec.Meta(ge=0)]


class PrivateCity(City):
    secret: str


class PydCity(BaseModel):
    population: Annotated[int, Field(ge=0)]


class AliasModel(BaseModel):
    internal: int = Field(alias='external')


OMITTED = object()


def setup(value, *, model=OMITTED, annotated=False, **options):
    router = MsgspecRouter()
    if model is not OMITTED:
        options['response_model'] = model
    if annotated:
        async def endpoint() -> City:
            return value
    else:
        async def endpoint():
            return value
    router.add_api_route('/value', endpoint, methods=['GET'], **options)
    app = FastAPI()
    app.include_router(router)
    install_openapi(app, router)
    return app


def response(app, path='/value'):
    return TestClient(app, raise_server_exceptions=False).get(path)


@pytest.mark.parametrize('kind', ['explicit', 'inferred'])
def test_declared_model_trusts_instance_like_pydantic(kind):
    # Same as FastAPI with a Pydantic response_model: an existing instance of the
    # declared class is not revalidated, so a later mutation is not detected.
    value = City(1)
    value.population = -1
    app = setup(value, model=City if kind == 'explicit' else OMITTED, annotated=kind == 'inferred')
    result = response(app)
    assert result.status_code == 200 and result.json() == {'population': -1}


def test_trusted_instance_encodes_nested_values_as_is():
    # Documented difference from Pydantic: only the top-level class is checked, so a
    # nested subclass inside a trusted instance keeps its extra fields.
    class Holder(msgspec.Struct):
        city: City

    app = setup(Holder(PrivateCity(1, 'PRIVATE')), model=Holder)
    assert response(app).json() == {'city': {'population': 1, 'secret': 'PRIVATE'}}


def test_trusted_generic_instance_does_not_check_type_parameter():
    # Documented difference from Pydantic: the type parameter of a generic Struct
    # instance is not checked, only its class.
    T = TypeVar('T')

    class Box(msgspec.Struct, Generic[T]):
        value: T

    app = setup(Box('text'), model=Box[int])
    assert response(app).json() == {'value': 'text'}


@pytest.mark.parametrize('annotated,model', [(True, None), (False, None), (False, OMITTED)])
def test_no_effective_model_serializes_mutated_struct(annotated, model):
    value = City(1)
    value.population = -1
    result = response(setup(value, model=model, annotated=annotated))
    assert result.status_code == 200 and result.json() == {'population': -1}


@pytest.mark.parametrize('model,status', [(City, 500), (None, 200), (OMITTED, 200)])
def test_raw_dict_constraints_follow_model_selection(model, status):
    result = response(setup({'population': -1}, model=model))
    assert result.status_code == status
    if status == 200:
        assert result.json() == {'population': -1}


def test_no_model_disables_field_projection():
    value = PrivateCity(1, 'PRIVATE')
    assert response(setup(value, model=City)).json() == {'population': 1}
    assert response(setup(value, model=None, annotated=True)).json() == {'population': 1, 'secret': 'PRIVATE'}


@pytest.mark.parametrize('annotated,model,has_schema', [(True, OMITTED, True), (False, City, True), (True, None, False), (False, OMITTED, False)])
def test_openapi_reflects_effective_response_model(annotated, model, has_schema):
    app = setup(City(1), annotated=annotated, model=model)
    schema = app.openapi()['paths']['/value']['get']['responses']['200']['content']['application/json']['schema']
    if has_schema:
        assert schema['$ref'].endswith('/City')
    else:
        assert schema == {}


@pytest.mark.parametrize('model', [City, None, OMITTED])
def test_explicit_response_bypasses_model_processing(model):
    result = response(setup(Response(b'ready', status_code=203, headers={'X-Ready': 'yes'}), model=model))
    assert result.status_code == 203 and result.content == b'ready'
    assert result.headers['X-Ready'] == 'yes'


@pytest.mark.parametrize('model', [City, None, OMITTED])
def test_included_router_keeps_status_and_headers(model):
    router = MsgspecRouter()
    options = {} if model is OMITTED else {'response_model': model}

    @router.get('/value', status_code=201, **options)
    async def endpoint(response: Response):
        response.status_code = 202
        response.headers['X-Endpoint'] = 'kept'
        return City(1)

    parent = MsgspecRouter()
    parent.include_router(router, prefix='/child')
    app = FastAPI()
    app.include_router(parent, prefix='/api')
    install_openapi(app, parent)
    result = response(app, '/api/child/value')
    assert result.status_code == 202 and result.json() == {'population': 1}
    assert result.headers['X-Endpoint'] == 'kept'


@pytest.mark.parametrize('model', [None, OMITTED])
def test_input_validation_remains_enabled_without_output_model(model):
    router = MsgspecRouter()
    options = {} if model is OMITTED else {'response_model': model}

    @router.post('/value', **options)
    async def endpoint(city: City):
        return city

    app = FastAPI()
    app.include_router(router)
    install_openapi(app, router)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post('/value', json={'population': -1}).status_code == 422
    assert client.post('/value', content=b'{', headers={'Content-Type': 'application/json'}).status_code == 400
    assert client.post('/value', json={'population': 1}).json() == {'population': 1}


@pytest.mark.parametrize('cls', [PlainTextResponse, HTMLResponse])
@pytest.mark.parametrize('at_router', [False, True])
def test_untyped_custom_response_classes_remain_delegated(cls, at_router):
    router = MsgspecRouter(**({'default_response_class': cls} if at_router else {}))
    options = {} if at_router else {'response_class': cls}

    @router.get('/value', **options)
    async def endpoint():
        return '<b>hello</b>'

    app = FastAPI()
    app.include_router(router)
    result = response(app)
    assert result.status_code == 200 and result.text == '<b>hello</b>'
    assert result.headers['Content-Type'].startswith(cls.media_type)


@pytest.mark.parametrize('status', [204, 304])
def test_untyped_bodyless_status_keeps_empty_body(status):
    result = response(setup(None, status_code=status))
    assert result.status_code == status and result.content == b''
    assert 'content-length' not in result.headers


@pytest.mark.parametrize('nested', [False, True])
def test_untyped_pydantic_values_use_public_json_aliases(nested):
    value = AliasModel(external=1)
    result = response(setup({'items': [value]} if nested else value))
    assert result.status_code == 200
    assert result.json() == ({'items': [{'external': 1}]} if nested else {'external': 1})


def test_annotated_pydantic_route_is_still_delegated():
    router = MsgspecRouter()

    @router.get('/value')
    async def endpoint() -> PydCity:
        return {'population': -1}

    app = FastAPI()
    app.include_router(router)
    assert response(app).status_code == 500


def test_revalidation_flag_is_not_an_api():
    with pytest.raises(TypeError):
        MsgspecRouter(revalidate_instances='never')


@pytest.mark.parametrize('model', [None, OMITTED])
@pytest.mark.parametrize('options', [
    {'response_model_include': {'population'}},
    {'response_model_exclude': {'extra'}},
    {'response_model_exclude_unset': True},
    {'response_model_exclude_defaults': True},
    {'response_model_exclude_none': True},
    {'response_model_by_alias': False},
])
def test_without_output_model_ignores_model_specific_options(model, options):
    value = {'population': 1, 'extra': None}
    result = response(setup(value, model=model, **options))
    assert result.status_code == 200 and result.json() == value


@pytest.mark.parametrize('options', [
    {'response_model_exclude_none': True}, {'response_model_by_alias': False},
])
def test_typed_response_still_rejects_unsupported_model_options(options):
    with pytest.raises(TypeError):
        setup(City(1), model=City, **options)
