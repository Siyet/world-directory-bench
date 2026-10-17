"""Layer boundaries and validation ordering, without a database dependency."""
from contextlib import asynccontextmanager
from dataclasses import replace
import subprocess
import sys

import pytest

from directory.dto import CityRow
from directory.presentation import output_preparer
from directory.responses import TRUSTED
from directory.schema_ops import schemas as s
from directory.service import DirectoryService


CITY = CityRow('local:city:fixture', 'local', 'City', 'local:country:fixture',
               None, 1.0, 2.0, 42, 'PPL')


class CacheControl:
    def __init__(self, cached=None):
        self.cached = cached
        self.writes = []
        self.gets = 0

    async def get(self, key):
        self.gets += 1
        return self.cached

    async def set(self, key, value, *, expire):
        self.writes.append((key, value, expire))


class RepositoryControl:
    def __init__(self, record):
        self.record = record
        self.reads = 0
        self.versions = 0
        self.active = False

    @staticmethod
    def resource_name(row_type):
        assert row_type is CityRow
        return 'cities'

    @asynccontextmanager
    async def transaction(self, *, readonly):
        assert readonly
        self.active = True
        try:
            yield self
        finally:
            self.active = False

    async def version(self):
        assert self.active
        self.versions += 1
        return 7

    async def read(self, row_type, query, record_id):
        assert self.active and row_type is CityRow and record_id == CITY.id
        self.reads += 1
        return [self.record]


def test_repository_and_service_import_without_http_contract_dependencies():
    subprocess.run([sys.executable, '-c', '''
import sys
import directory.repository, directory.service, directory.dto
assert not any(name in sys.modules for name in (
    'fastapi', 'pydantic', 'msgspec', 'directory.schemas',
    'directory.msgspec_schemas', 'directory.presentation'))
'''], check=True)


@pytest.mark.asyncio
async def test_invalid_database_dto_is_not_cached():
    if TRUSTED:
        pytest.skip('trusted control deliberately omits output validation')
    repository = RepositoryControl(replace(CITY, feature_code='INVALID'))
    cache = CacheControl()
    service = DirectoryService(repository, cache)
    with pytest.raises(ValueError):
        await service.read(CityRow, record_id=CITY.id, prepare_output=output_preparer(s.City))
    assert repository.reads == repository.versions == 1
    assert cache.writes == [] and not repository.active


@pytest.mark.asyncio
async def test_corrupt_cache_is_validated_inside_read_transaction():
    if TRUSTED:
        pytest.skip('trusted control deliberately omits output validation')
    repository = RepositoryControl(CITY)
    cache = CacheControl({'id': CITY.id, 'unexpected': 'invalid-cache-marker'})
    service = DirectoryService(repository, cache)
    with pytest.raises(ValueError):
        await service.read(CityRow, record_id=CITY.id, prepare_output=output_preparer(s.City))
    assert repository.versions == 1 and repository.reads == 0
    assert cache.writes == [] and not repository.active


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [False, True])
async def test_preparation_and_cache_serialization_order(enabled):
    repository = RepositoryControl(CITY)
    cache = CacheControl()
    service = DirectoryService(repository, cache, cache_enabled=enabled)
    prepare = output_preparer(s.City)
    calls = []

    def observed(value, for_cache):
        assert repository.active and repository.versions == 1
        assert not cache.writes
        result = prepare(value, for_cache)
        calls.append(for_cache)
        assert (result.cache_payload is not None) is for_cache
        return result

    result, state = await service.read(CityRow, record_id=CITY.id, prepare_output=observed)
    assert calls == [enabled]
    assert state == ('miss' if enabled else 'bypass')
    assert cache.gets == int(enabled) and len(cache.writes) == int(enabled)
    if enabled:
        key, payload, ttl = cache.writes[0]
        assert key.startswith('wd:v1:7:cities:') and ttl == 60
        assert payload['id'] == CITY.id and payload['population'] == 42
        assert type(payload) is dict


@pytest.mark.asyncio
async def test_bypass_does_not_serialize_cache_payload(monkeypatch):
    import directory.presentation as presentation
    repository = RepositoryControl(CITY)
    service = DirectoryService(repository, CacheControl(), cache_enabled=False)

    def forbidden(*args, **kwargs):
        raise AssertionError('Unneeded cache serialization in bypass mode')

    monkeypatch.setattr(presentation, 'data', forbidden)
    await service.read(CityRow, record_id=CITY.id, prepare_output=output_preparer(s.City))
