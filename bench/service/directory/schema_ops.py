"""Select the validation implementation without changing the SQL/cache contract."""
import os

MSGSPEC = os.environ.get('RESPONSE_PATH') in {'msgspec', 'msgspec-json-response'}
if MSGSPEC:
    import msgspec
    from . import msgspec_schemas as schemas
else:
    from . import schemas


def data(value, *, exclude_none=False):
    result = msgspec.to_builtins(value) if MSGSPEC else value.model_dump()
    return {key: item for key, item in result.items() if item is not None} if exclude_none else result


def fields(schema):
    return schema.__struct_fields__ if MSGSPEC else schema.model_fields


def validate(schema, value, *, from_attributes=False):
    if MSGSPEC:
        return msgspec.convert(value, type=schema, strict=True, from_attributes=from_attributes)
    return schema.model_validate(value)


def validate_json(schema, value):
    if MSGSPEC:
        return msgspec.json.decode(value, type=schema)
    return schema.model_validate_json(value)
