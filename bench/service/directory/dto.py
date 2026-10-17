"""Database row values, independent of HTTP contract validation and serialization."""
from dataclasses import dataclass, fields


@dataclass(frozen=True, slots=True)
class CountryRow:
    id: str
    source: str
    name: str
    country_code: str
    continent: str


@dataclass(frozen=True, slots=True)
class DivisionRow:
    id: str
    source: str
    name: str
    country_id: str
    code: str
    level: int
    parent_id: str | None


@dataclass(frozen=True, slots=True)
class CityRow:
    id: str
    source: str
    name: str
    country_id: str
    division_id: str | None
    latitude: float
    longitude: float
    population: int
    feature_code: str


@dataclass(frozen=True, slots=True)
class AirportRow:
    id: str
    source: str
    name: str
    country_id: str
    region_id: str | None
    city_id: str | None
    latitude: float
    longitude: float
    airport_type: str
    scheduled_service: bool
    municipality: str | None
    iata_code: str | None


RecordRow = CountryRow | DivisionRow | CityRow | AirportRow


@dataclass(frozen=True, slots=True)
class PageRows:
    items: list[RecordRow]
    next_after_id: str | None


def row_data(record: RecordRow) -> dict:
    # All database columns are scalar values; avoid recursive copying.
    return {field.name: getattr(record, field.name) for field in fields(record)}
