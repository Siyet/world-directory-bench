from typing import Annotated, ClassVar, Generic, Literal, TypeVar

import msgspec
from msgspec import Meta


def valid_text(value):
    if isinstance(value, str):
        if '\x00' in value:
            raise ValueError('NUL is not permitted')
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            raise ValueError('Unicode scalar values required') from None

Source = Literal['geonames', 'ourairports', 'local']
Identifier = Annotated[str, Meta(min_length=1, max_length=160)]
Name = Annotated[str, Meta(min_length=1, max_length=200)]
CountryCode = str
Population = Annotated[int, Meta(ge=0, le=9223372036854775807)]
Latitude = Annotated[float, Meta(ge=-90, le=90)]
Longitude = Annotated[float, Meta(ge=-180, le=180)]
AirportType = Literal['small_airport', 'medium_airport', 'large_airport', 'heliport', 'seaplane_base', 'balloonport', 'closed']


class Record(msgspec.Struct, forbid_unknown_fields=True):
    kind: ClassVar[str]
    id: Identifier
    source: Source
    name: Name

    def __post_init__(self):
        for field in self.__struct_fields__:
            valid_text(getattr(self, field))
        if not self.id.startswith(f'{self.source}:{self.kind}:'):
            raise ValueError('id namespace does not match source and resource')


class Country(Record):
    kind = 'country'
    country_code: CountryCode
    continent: Literal['AF', 'AN', 'AS', 'EU', 'NA', 'OC', 'SA']


class Division(Record):
    kind = 'division'
    country_id: Identifier
    code: Annotated[str, Meta(min_length=1, max_length=100)]
    level: Annotated[int, Meta(ge=1, le=5)]
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
    municipality: Annotated[str, Meta(max_length=200)] | None
    iata_code: str | None


T = TypeVar('T', bound=Record)


class Page(msgspec.Struct, Generic[T], forbid_unknown_fields=True):
    items: list[T]
    next_after_id: str | None


class ListQuery(msgspec.Struct, forbid_unknown_fields=True):
    after_id: Identifier | None = None
    limit: Annotated[int, Meta(ge=1, le=1000)] = 100
    name_prefix: Annotated[str, Meta(min_length=1, max_length=200)] | None = None

    def __post_init__(self):
        valid_text(self.name_prefix)


class CountryQuery(ListQuery):
    country_code: CountryCode | None = None
    source: Source | None = None


class DivisionQuery(CountryQuery):
    parent_id: Identifier | None = None
    level: Annotated[int, Meta(ge=1, le=5)] | None = None


class CityQuery(CountryQuery):
    division_id: Identifier | None = None
    min_population: Population | None = None


class AirportQuery(CountryQuery):
    region_id: Identifier | None = None
    airport_type: AirportType | None = None
    scheduled_service: Literal['true', 'false'] | None = None



FeatureCode = Literal['PPL', 'PPLA', 'PPLA2', 'PPLA3', 'PPLA4', 'PPLA5', 'PPLC']


class PopulationRange(msgspec.Struct, forbid_unknown_fields=True):
    min: Population | None = None
    max: Population | None = None

    def __post_init__(self):
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError('min must not exceed max')


class CityFilter(msgspec.Struct, forbid_unknown_fields=True):
    country_codes: Annotated[list[CountryCode], Meta(min_length=1, max_length=50)] | None = None
    division_ids: Annotated[list[Identifier], Meta(min_length=1, max_length=50)] | None = None
    sources: Annotated[list[Literal['geonames', 'local']], Meta(min_length=1, max_length=2)] | None = None
    feature_codes: Annotated[list[FeatureCode], Meta(min_length=1, max_length=7)] | None = None
    population: PopulationRange | None = None
    name_prefix: Annotated[str, Meta(min_length=1, max_length=200)] | None = None

    def __post_init__(self):
        valid_text(self.name_prefix)


class CitySearch(msgspec.Struct, forbid_unknown_fields=True):
    """POST body: the input contract of the city search."""
    filter: CityFilter = msgspec.field(default_factory=CityFilter)
    order: Literal['id', '-id'] = 'id'
    after_id: Identifier | None = None
    limit: Annotated[int, Meta(ge=1, le=1000)] = 100


class CityBatch(msgspec.Struct, forbid_unknown_fields=True):
    """POST body of the ingest check: a batch of cities, the same contract as the GET page items."""
    items: Annotated[list[City], Meta(min_length=1, max_length=1000)]


class Accepted(msgspec.Struct, forbid_unknown_fields=True):
    accepted: int
