"""Load validated normalized JSONL into an empty database, atomically."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import func, insert, select, text, update

from . import models as m
from .schema_ops import schemas as s, data, validate_json
from .repository import Repository, MODELS
from .presentation import ROW_TYPES


def records(path, schema):
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            try:
                yield validate_json(schema, line)
            except ValueError as exc:
                raise ValueError(f'{path.name}:{number}: {exc}') from exc


async def load(path):
    repository = Repository(os.environ['DATABASE_URL'])
    schemas = {row_type: schema for schema, row_type in ROW_TYPES.items()}
    countries = {}
    divisions = {}
    report = {'counts': {}, 'sha256': {}}
    try:
        async with repository.sessions.begin() as session:
            await session.execute(text('LOCK TABLE countries, divisions, cities, airports IN SHARE ROW EXCLUSIVE MODE'))
            for row_type, model in MODELS.items():
                schema = schemas[row_type]
                if await session.scalar(select(func.count()).select_from(model)):
                    raise ValueError('Load requires all four resource tables to be empty')
            if await session.get(m.Version, 1) is None:
                raise ValueError('Initialize the database before loading')
            for row_type, model in MODELS.items():
                schema = schemas[row_type]
                file = path / (model.__tablename__ + '.jsonl')
                digest = hashlib.sha256()
                with file.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1048576), b''):
                        digest.update(chunk)
                report['sha256'][file.name] = digest.hexdigest()
                count = 0
                # Parent divisions precede their children even if source files are unsorted.
                levels = range(1, 6) if schema is s.Division else [None]
                for level in levels:
                    batch = []
                    for record in records(file, schema):
                        if level is not None and record.level != level:
                            continue
                        if isinstance(record, s.Country):
                            countries[record.id] = record.source
                        else:
                            country_source = countries.get(record.country_id)
                            if country_source is None or (record.source != 'local' and record.source != country_source):
                                raise ValueError(f'Invalid country/source reference: {record.id}')
                        if isinstance(record, s.Division):
                            if record.parent_id:
                                parent = divisions.get(record.parent_id)
                                if parent is None or parent[0] != record.country_id or parent[1] >= record.level:
                                    raise ValueError(f'Invalid parent division: {record.id}')
                            divisions[record.id] = (record.country_id, record.level)
                        division_id = getattr(record, 'division_id', None) or getattr(record, 'region_id', None)
                        if division_id and (division_id not in divisions or divisions[division_id][0] != record.country_id):
                            raise ValueError(f'Invalid division reference: {record.id}')
                        batch.append(data(record))
                        count += 1
                        if len(batch) == 1000:
                            await session.execute(insert(model), batch)
                            batch.clear()
                    if batch:
                        await session.execute(insert(model), batch)
                report['counts'][model.__tablename__] = count
            await session.execute(update(m.Version).where(m.Version.id == 1).values(value=m.Version.value + 1))
        return report
    finally:
        await repository.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('normalized_directory', type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(load(args.normalized_directory)), indent=2))
