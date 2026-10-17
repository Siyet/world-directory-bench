"""Collect PostgreSQL storage sizes when an authorized database run is available."""
import asyncio
import json
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


SIZE_SQL = """
SELECT name, pg_total_relation_size(name::regclass)::bigint AS bytes
FROM (VALUES ('countries'), ('divisions'), ('cities'), ('airports'),
             ('dataset_version')) AS tables(name)
ORDER BY name
"""


async def collect(database_url):
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = (await connection.execute(text(SIZE_SQL))).mappings().all()
            database_bytes = await connection.scalar(text('SELECT pg_database_size(current_database())'))
        return {
            'tables': {row['name']: {'bytes': row['bytes'], 'MB_decimal': row['bytes'] / 1_000_000}
                       for row in rows},
            'database': {'bytes': database_bytes, 'MB_decimal': database_bytes / 1_000_000},
            'scope': 'pg_total_relation_size includes indexes and TOAST; database size also includes other relations. MB = 1000000 bytes.',
        }
    finally:
        await engine.dispose()


if __name__ == '__main__':
    print(json.dumps(asyncio.run(collect(os.environ['DATABASE_URL'])), indent=2))
