"""Narrow city GET experiment, using the same database DTO and transaction policy."""
import asyncio
from contextlib import asynccontextmanager

import asyncpg

from directory.dto import CityRow

COLUMNS = ('id', 'source', 'name', 'country_id', 'division_id',
           'latitude', 'longitude', 'population', 'feature_code')
SELECT_CITY = 'SELECT ' + ', '.join('cities.' + field for field in COLUMNS) + ' FROM cities'


class Repository:
    def __init__(self, database_url):
        self.database_url = database_url.replace('postgresql+asyncpg://', 'postgresql://', 1)
        self._pool = None
        self._pool_lock = asyncio.Lock()

    @staticmethod
    def resource_name(row_type):
        if row_type is not CityRow:
            raise NotImplementedError('This experiment covers only city GET routes')
        return 'cities'

    async def _get_pool(self):
        if self._pool is None:
            async with self._pool_lock:
                if self._pool is None:
                    self._pool = await asyncpg.create_pool(
                        self.database_url, min_size=0, max_size=10,
                        statement_cache_size=100,
                    )
        return self._pool

    async def close(self):
        if self._pool is not None:
            await self._pool.close()

    @asynccontextmanager
    async def transaction(self, *, readonly=False):
        if not readonly:
            raise NotImplementedError('This experiment covers read-only city operations')
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            # Separate BEGIN and SET statements match the ORM control.
            async with connection.transaction():
                await connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                yield RepositoryTransaction(connection)
        # Pool release retains asyncpg's normal connection reset.


class RepositoryTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def version(self):
        value = await self.connection.fetchval(
            'SELECT dataset_version.value FROM dataset_version WHERE dataset_version.id = $1::INTEGER', 1,
        )
        if value is None:
            raise RuntimeError('Initialize the database before starting the API')
        return value

    async def read(self, row_type, query=None, record_id=None):
        if row_type is not CityRow:
            raise NotImplementedError('This experiment covers only city GET routes')
        statement, args = city_statement(query, record_id)
        records = await self.connection.fetch(statement, *args)
        return [CityRow(*record) for record in records]


def city_statement(query=None, record_id=None):
    statement = SELECT_CITY
    clauses = []
    args = []

    def parameter(value, sql_type):
        args.append(value)
        return f'${len(args)}::{sql_type}'

    if record_id is not None:
        clauses.append('cities.id = ' + parameter(record_id, 'VARCHAR'))
    elif 'filter' in query:
        return search_statement(query)
    else:
        filters = {key: value for key, value in query.items() if value is not None}
        if 'country_code' in filters:
            statement += ' JOIN countries ON cities.country_id = countries.id'
            clauses.append('countries.country_code = ' + parameter(filters['country_code'], 'VARCHAR'))
        for field in ('source', 'division_id'):
            if field in filters:
                clauses.append('cities.' + field + ' = ' + parameter(filters[field], 'VARCHAR'))
        if 'min_population' in filters:
            clauses.append('cities.population >= ' + parameter(filters['min_population'], 'BIGINT'))
        if 'name_prefix' in filters:
            escaped = filters['name_prefix'].replace('/', '//').replace('%', '/%').replace('_', '/_')
            clauses.append("cities.name LIKE (" + parameter(escaped, 'VARCHAR') + " || '%') ESCAPE '/'")
        if 'after_id' in filters:
            clauses.append('cities.id > ' + parameter(filters['after_id'], 'VARCHAR'))
    if clauses:
        statement += ' WHERE ' + ' AND '.join(clauses)
    if record_id is None:
        statement += ' ORDER BY cities.id LIMIT ' + parameter(query['limit'] + 1, 'INTEGER')
    return statement, args


def search_statement(query):
    """City search from the POST body, the same clauses as the ORM search statement."""
    statement = SELECT_CITY
    clauses = []
    args = []

    def parameter(value, sql_type):
        args.append(value)
        return f'${len(args)}::{sql_type}'

    filters = {key: value for key, value in query['filter'].items() if value is not None}
    if 'country_codes' in filters:
        statement += ' JOIN countries ON cities.country_id = countries.id'
        clauses.append('countries.country_code = ANY(' + parameter(filters['country_codes'], 'VARCHAR[]') + ')')
    for field, column in (('division_ids', 'division_id'), ('sources', 'source'), ('feature_codes', 'feature_code')):
        if field in filters:
            clauses.append('cities.' + column + ' = ANY(' + parameter(filters[field], 'VARCHAR[]') + ')')
    population = filters.get('population') or {}
    if population.get('min') is not None:
        clauses.append('cities.population >= ' + parameter(population['min'], 'BIGINT'))
    if population.get('max') is not None:
        clauses.append('cities.population <= ' + parameter(population['max'], 'BIGINT'))
    if 'name_prefix' in filters:
        escaped = filters['name_prefix'].replace('/', '//').replace('%', '/%').replace('_', '/_')
        clauses.append("cities.name LIKE (" + parameter(escaped, 'VARCHAR') + " || '%') ESCAPE '/'")
    descending = query['order'] == '-id'
    if query['after_id'] is not None:
        clauses.append('cities.id ' + ('<' if descending else '>') + ' ' + parameter(query['after_id'], 'VARCHAR'))
    if clauses:
        statement += ' WHERE ' + ' AND '.join(clauses)
    statement += ' ORDER BY cities.id' + (' DESC' if descending else '') + ' LIMIT ' + parameter(query['limit'] + 1, 'INTEGER')
    return statement, args
