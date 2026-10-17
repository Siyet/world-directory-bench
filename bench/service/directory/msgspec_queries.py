"""Flat HTTP dependencies for the adapter's supported scalar parameter contract."""
from typing import Annotated, Literal
import msgspec
from fastapi import HTTPException, Request
from . import msgspec_schemas as s


def query_value(request, schema, values):
    if set(request.query_params) - set(values):
        raise HTTPException(422, 'invalid_fields')
    try:
        return schema(**values)
    except ValueError:
        raise HTTPException(422, 'invalid_fields') from None


async def country_query(
    request: Request,
    after_id: s.Identifier | None = None,
    limit: Annotated[int, msgspec.Meta(ge=1, le=1000)] = 100,
    name_prefix: s.Name | None = None,
    country_code: s.CountryCode | None = None,
    source: s.Source | None = None,
) -> s.CountryQuery:
    return query_value(request, s.CountryQuery, {'after_id': after_id, 'limit': limit, 'name_prefix': name_prefix, 'country_code': country_code, 'source': source})

async def division_query(
    request: Request,
    after_id: s.Identifier | None = None,
    limit: Annotated[int, msgspec.Meta(ge=1, le=1000)] = 100,
    name_prefix: s.Name | None = None,
    country_code: s.CountryCode | None = None,
    source: s.Source | None = None,
    parent_id: s.Identifier | None = None,
    level: Annotated[int, msgspec.Meta(ge=1, le=5)] | None = None,
) -> s.DivisionQuery:
    return query_value(request, s.DivisionQuery, {'after_id': after_id, 'limit': limit, 'name_prefix': name_prefix, 'country_code': country_code, 'source': source, 'parent_id': parent_id, 'level': level})

async def city_query(
    request: Request,
    after_id: s.Identifier | None = None,
    limit: Annotated[int, msgspec.Meta(ge=1, le=1000)] = 100,
    name_prefix: s.Name | None = None,
    country_code: s.CountryCode | None = None,
    source: s.Source | None = None,
    division_id: s.Identifier | None = None,
    min_population: s.Population | None = None,
) -> s.CityQuery:
    return query_value(request, s.CityQuery, {'after_id': after_id, 'limit': limit, 'name_prefix': name_prefix, 'country_code': country_code, 'source': source, 'division_id': division_id, 'min_population': min_population})

async def airport_query(
    request: Request,
    after_id: s.Identifier | None = None,
    limit: Annotated[int, msgspec.Meta(ge=1, le=1000)] = 100,
    name_prefix: s.Name | None = None,
    country_code: s.CountryCode | None = None,
    source: s.Source | None = None,
    region_id: s.Identifier | None = None,
    airport_type: s.AirportType | None = None,
    scheduled_service: Literal['true', 'false'] | None = None,
) -> s.AirportQuery:
    return query_value(request, s.AirportQuery, {'after_id': after_id, 'limit': limit, 'name_prefix': name_prefix, 'country_code': country_code, 'source': source, 'region_id': region_id, 'airport_type': airport_type, 'scheduled_service': scheduled_service})
