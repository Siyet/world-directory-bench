from typing import Annotated, ClassVar, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Source = Literal['geonames', 'ourairports', 'local']
Identifier = Annotated[str, Field(min_length=1, max_length=160)]
Name = Annotated[str, Field(min_length=1, max_length=200)]
CountryCode = str
Population = Annotated[int, Field(ge=0, le=9223372036854775807)]
Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]
AirportType = Literal['small_airport', 'medium_airport', 'large_airport', 'heliport', 'seaplane_base', 'balloonport', 'closed']


class Record(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid', from_attributes=True, allow_inf_nan=False)
    kind: ClassVar[str]
    id: Identifier
    source: Source
    name: Name

    @field_validator('*')
    @classmethod
    def unicode_scalar_text(cls, value):
        if isinstance(value, str):
            if '\x00' in value:
                raise ValueError('NUL is not permitted')
            try:
                value.encode('utf-8')
            except UnicodeEncodeError:
                raise ValueError('Unicode scalar values required') from None
        return value

    @model_validator(mode='after')
    def namespace(self):
        if not self.id.startswith(f'{self.source}:{self.kind}:'):
            raise ValueError('id namespace does not match source and resource')
        return self


class Country(Record):
    kind = 'country'
    country_code: CountryCode
    continent: Literal['AF', 'AN', 'AS', 'EU', 'NA', 'OC', 'SA']


class Division(Record):
    kind = 'division'
    country_id: Identifier
    code: Annotated[str, Field(min_length=1, max_length=100)]
    level: Annotated[int, Field(ge=1, le=5)]
    parent_id: Identifier | None


class City(Record):
    kind = 'city'
    source: Literal['geonames', 'local']
    country_id: Identifier
    division_id: Identifier | None
    latitude: Latitude
    longitude: Longitude
    population: Population
    feature_code: Literal['PPL', 'PPLA', 'PPLA2', 'PPLA3', 'PPLA4', 'PPLA5', 'PPLC']


class Airport(Record):
    kind = 'airport'
    source: Literal['ourairports', 'local']
    country_id: Identifier
    region_id: Identifier | None
    city_id: Identifier | None
    latitude: Latitude
    longitude: Longitude
    airport_type: AirportType
    scheduled_service: bool
    municipality: Annotated[str, Field(max_length=200)] | None
    iata_code: str | None


T = TypeVar('T', bound=Record)


class Page(BaseModel, Generic[T]):
    model_config = ConfigDict(strict=True, extra='forbid')
    items: list[T]
    next_after_id: str | None


class ListQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    after_id: Identifier | None = None
    limit: Annotated[int, Field(ge=1, le=1000)] = 100
    name_prefix: Annotated[str, Field(min_length=1, max_length=200)] | None = None

    @field_validator('name_prefix')
    @classmethod
    def valid_text(cls, value):
        return Record.unicode_scalar_text(value)


class CountryQuery(ListQuery):
    country_code: CountryCode | None = None
    source: Source | None = None


class DivisionQuery(CountryQuery):
    parent_id: Identifier | None = None
    level: Annotated[int, Field(ge=1, le=5)] | None = None


class CityQuery(CountryQuery):
    division_id: Identifier | None = None
    min_population: Population | None = None


class AirportQuery(CountryQuery):
    region_id: Identifier | None = None
    airport_type: AirportType | None = None
    scheduled_service: Literal['true', 'false'] | None = None


class Error(BaseModel):
    error: str


FeatureCode = Literal['PPL', 'PPLA', 'PPLA2', 'PPLA3', 'PPLA4', 'PPLA5', 'PPLC']


class SearchModel(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')


class PopulationRange(SearchModel):
    min: Population | None = None
    max: Population | None = None

    @model_validator(mode='after')
    def ordered(self):
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError('min must not exceed max')
        return self


class CityFilter(SearchModel):
    country_codes: Annotated[list[CountryCode], Field(min_length=1, max_length=50)] | None = None
    division_ids: Annotated[list[Identifier], Field(min_length=1, max_length=50)] | None = None
    sources: Annotated[list[Literal['geonames', 'local']], Field(min_length=1, max_length=2)] | None = None
    feature_codes: Annotated[list[FeatureCode], Field(min_length=1, max_length=7)] | None = None
    population: PopulationRange | None = None
    name_prefix: Annotated[str, Field(min_length=1, max_length=200)] | None = None

    @field_validator('name_prefix')
    @classmethod
    def valid_text(cls, value):
        return Record.unicode_scalar_text(value)


class CitySearch(SearchModel):
    """POST body: the input contract of the city search."""
    filter: CityFilter = CityFilter()
    order: Literal['id', '-id'] = 'id'
    after_id: Identifier | None = None
    limit: Annotated[int, Field(ge=1, le=1000)] = 100


class CityBatch(SearchModel):
    """POST body of the ingest check: a batch of cities, the same contract as the GET page items."""
    items: Annotated[list[City], Field(min_length=1, max_length=1000)]


class Accepted(SearchModel):
    accepted: int
