"""Reference rules and generation-aware cache orchestration."""
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json

from .dto import CountryRow, DivisionRow, PageRows
from .errors import Conflict, NotFound


@dataclass(slots=True)
class PreparedOutput:
    value: object
    cache_payload: dict | None


# The presentation callback validates before cache writes. No HTTP schema is
# imported here. Its second argument requests primitive cache serialization.
PrepareOutput = Callable[[object, bool], PreparedOutput]


class DirectoryService:
    def __init__(self, repository, cache, *, cache_enabled=True):
        self.repository = repository
        self.cache = cache
        self.cache_enabled = cache_enabled

    async def close(self):
        try:
            await self.cache.close()
        finally:
            await self.repository.close()

    async def read(self, row_type, query=None, record_id=None, *, prepare_output: PrepareOutput):
        params = query if query is not None else {'id': record_id}
        digest = hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        async with self.repository.transaction(readonly=True) as transaction:
            version = await transaction.version()
            key = f'wd:v1:{version}:{self.repository.resource_name(row_type)}:{digest}'
            if self.cache_enabled:
                cached = await self.cache.get(key)
                if cached is not None:
                    return prepare_output(cached, False).value, 'hit'
            records = await transaction.read(row_type, query, record_id)
            if record_id is not None:
                if not records:
                    raise NotFound()
                result = records[0]
            else:
                limit = query['limit']
                after_id = records[limit - 1].id if len(records) > limit else None
                result = PageRows(records[:limit], after_id)
            prepared = prepare_output(result, self.cache_enabled)
            if self.cache_enabled:
                await self.cache.set(key, prepared.cache_payload, expire=60)
            return prepared.value, 'miss' if self.cache_enabled else 'bypass'

    async def validate_links(self, transaction, record):
        if isinstance(record, CountryRow):
            return
        country = await transaction.get(CountryRow, record.country_id)
        if country is None or (record.source != 'local' and country.source != record.source):
            raise Conflict('invalid_reference')
        if isinstance(record, DivisionRow) and record.parent_id:
            parent = await transaction.get(DivisionRow, record.parent_id)
            if parent is None or parent.country_id != record.country_id or parent.level >= record.level:
                raise Conflict('invalid_reference')
        division_id = getattr(record, 'division_id', None) or getattr(record, 'region_id', None)
        if division_id:
            division = await transaction.get(DivisionRow, division_id)
            if division is None or division.country_id != record.country_id:
                raise Conflict('invalid_reference')

    async def save(self, record, replace_id=None):
        async with self.repository.transaction() as transaction:
            if replace_id is not None:
                existing = await transaction.get(type(record), replace_id, for_update=True)
                if existing is None:
                    raise NotFound()
                immutable = ('id', 'source', 'level') if isinstance(record, DivisionRow) else ('id', 'source')
                if any(getattr(existing, key) != getattr(record, key) for key in immutable):
                    raise Conflict('immutable_identity')
            # No pending ORM mutation exists while reference rows are checked.
            await self.validate_links(transaction, record)
            if replace_id is None:
                transaction.add(record)
            else:
                transaction.replace(record, replace_id)
            await transaction.finish_mutation()
        return record

    async def delete(self, row_type, record_id):
        async with self.repository.transaction() as transaction:
            if await transaction.get(row_type, record_id, for_update=True) is None:
                raise NotFound()
            await transaction.delete(row_type, record_id)
            await transaction.finish_mutation()
