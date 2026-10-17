"""Mapping between database row DTOs and validated HTTP contract schemas."""
from .dto import CountryRow, DivisionRow, CityRow, AirportRow, PageRows, row_data
from .responses import TRUSTED, RESPONSE_PATH
from .schema_ops import schemas as s, data, validate
from .service import PreparedOutput

ROW_TYPES = {s.Country: CountryRow, s.Division: DivisionRow, s.City: CityRow, s.Airport: AirportRow}


def input_row(body):
    return ROW_TYPES[type(body)](**data(body))


def output_preparer(schema, *, page=False):
    """One callback keeps output validation before a service cache write."""
    def prepare(value, for_cache):
        if isinstance(value, dict):
            result = value if TRUSTED else validate(s.Page[schema] if page else schema, value)
        elif isinstance(value, PageRows):
            items = [row_data(row) if TRUSTED else validate(schema, row, from_attributes=True) for row in value.items]
            content = {'items': items, 'next_after_id': value.next_after_id}
            if TRUSTED:
                result = content
            elif RESPONSE_PATH == 'msgspec-json-response':
                result = validate(s.Page[schema], content)
            else:
                result = s.Page[schema](**content)
        else:
            result = row_data(value) if TRUSTED else validate(schema, value, from_attributes=True)
        payload = (result if TRUSTED else data(result)) if for_cache else None
        return PreparedOutput(result, payload)
    return prepare
