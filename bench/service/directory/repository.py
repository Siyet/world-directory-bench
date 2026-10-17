"""SQL persistence and transaction ownership; no API contract schemas."""
from contextlib import asynccontextmanager
from dataclasses import fields
from operator import attrgetter

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from . import models as m
from .dto import CountryRow, DivisionRow, CityRow, AirportRow, row_data
from .errors import Conflict

MODELS = {CountryRow: m.Country, DivisionRow: m.Division, CityRow: m.City, AirportRow: m.Airport}


# Field getters are computed once per DTO type; DTOs are built positionally in field order.
_GETTERS = {row_type: attrgetter(*(field.name for field in fields(row_type))) for row_type in MODELS}


def to_row(row_type, record):
    return row_type(*_GETTERS[row_type](record))


def search_statement(model, query):
    """City search from the POST body: list filters, a range, a prefix and keyset order."""
    statement = select(model)
    filters = {key: value for key, value in query['filter'].items() if value is not None}
    if 'country_codes' in filters:
        statement = statement.join(m.Country, model.country_id == m.Country.id).where(m.Country.country_code.in_(filters['country_codes']))
    for field, column in (('division_ids', model.division_id), ('sources', model.source), ('feature_codes', model.feature_code)):
        if field in filters:
            statement = statement.where(column.in_(filters[field]))
    population = filters.get('population') or {}
    if population.get('min') is not None:
        statement = statement.where(model.population >= population['min'])
    if population.get('max') is not None:
        statement = statement.where(model.population <= population['max'])
    if 'name_prefix' in filters:
        statement = statement.where(model.name.startswith(filters['name_prefix'], autoescape=True))
    descending = query['order'] == '-id'
    if query['after_id'] is not None:
        statement = statement.where(model.id < query['after_id'] if descending else model.id > query['after_id'])
    return statement.order_by(model.id.desc() if descending else model.id).limit(query['limit'] + 1)


class Repository:
    def __init__(self, database_url):
        self.engine = create_async_engine(database_url, pool_size=10, max_overflow=0)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def resource_name(row_type):
        return MODELS[row_type].__tablename__

    async def close(self):
        await self.engine.dispose()

    @asynccontextmanager
    async def transaction(self, *, readonly=False):
        try:
            async with self.sessions.begin() as session:
                if readonly:
                    await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
                yield RepositoryTransaction(session)
        except IntegrityError:
            raise Conflict() from None


class RepositoryTransaction:
    def __init__(self, session):
        self.session = session
        self._locked = {}

    async def version(self):
        version = await self.session.scalar(select(m.Version.value).where(m.Version.id == 1))
        if version is None:
            raise RuntimeError('Initialize the database before starting the API')
        return version

    async def get(self, row_type, record_id, *, for_update=False):
        model = MODELS[row_type]
        if for_update:
            record = await self.session.scalar(select(model).where(model.id == record_id).with_for_update())
            if record is not None:
                self._locked[(row_type, record_id)] = record
        else:
            record = await self.session.get(model, record_id)
        return None if record is None else to_row(row_type, record)

    async def read(self, row_type, query=None, record_id=None):
        model = MODELS[row_type]
        statement = select(model)
        if record_id is not None:
            statement = statement.where(model.id == record_id)
        elif 'filter' in query:
            statement = search_statement(model, query)
        else:
            filters = {key: value for key, value in query.items() if value is not None}
            if 'country_code' in filters:
                if model is m.Country:
                    statement = statement.where(m.Country.country_code == filters['country_code'])
                else:
                    statement = statement.join(m.Country, model.country_id == m.Country.id).where(m.Country.country_code == filters['country_code'])
            for field in ('source', 'parent_id', 'level', 'division_id', 'region_id', 'airport_type'):
                if field in filters:
                    statement = statement.where(getattr(model, field) == filters[field])
            if 'scheduled_service' in filters:
                statement = statement.where(model.scheduled_service == (filters['scheduled_service'] == 'true'))
            if 'min_population' in filters:
                statement = statement.where(model.population >= filters['min_population'])
            if 'name_prefix' in filters:
                statement = statement.where(model.name.startswith(filters['name_prefix'], autoescape=True))
            if 'after_id' in filters:
                statement = statement.where(model.id > filters['after_id'])
            statement = statement.order_by(model.id).limit(query['limit'] + 1)
        records = (await self.session.scalars(statement)).all()
        return [to_row(row_type, record) for record in records]

    def add(self, record):
        self.session.add(MODELS[type(record)](**row_data(record)))

    def replace(self, record, record_id):
        existing = self._locked[(type(record), record_id)]
        for key, value in row_data(record).items():
            setattr(existing, key, value)

    async def delete(self, row_type, record_id):
        await self.session.delete(self._locked[(row_type, record_id)])

    async def finish_mutation(self):
        await self.session.flush()
        await self.session.execute(update(m.Version).where(m.Version.id == 1).values(value=m.Version.value + 1))
