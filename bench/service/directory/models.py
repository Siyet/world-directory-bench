from sqlalchemy import BigInteger, Boolean, CheckConstraint, Double, ForeignKey, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Version(Base):
    __tablename__ = 'dataset_version'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (CheckConstraint('id = 1'),)


class Named:
    id: Mapped[str] = mapped_column(String(160, collation='C'), primary_key=True)
    source: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200, collation='C'))


class Country(Named, Base):
    __tablename__ = 'countries'
    country_code: Mapped[str] = mapped_column(String(2))
    continent: Mapped[str] = mapped_column(String(2))
    __table_args__ = (
        UniqueConstraint('source', 'country_code'),
        Index('countries_code_id', 'country_code', 'id'),
        Index('countries_name_id', 'name', 'id'),
    )


class Division(Named, Base):
    __tablename__ = 'divisions'
    country_id: Mapped[str] = mapped_column(ForeignKey('countries.id'))
    code: Mapped[str] = mapped_column(String(100))
    level: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(String(160, collation='C'))
    __table_args__ = (
        UniqueConstraint('id', 'country_id'),
        UniqueConstraint('source', 'code'),
        ForeignKeyConstraint(['parent_id', 'country_id'], ['divisions.id', 'divisions.country_id']),
        CheckConstraint('level BETWEEN 1 AND 5'),
        Index('divisions_country_id', 'country_id', 'id'),
        Index('divisions_parent_id', 'parent_id', 'id'),
        Index('divisions_name_id', 'name', 'id'),
    )


class City(Named, Base):
    __tablename__ = 'cities'
    country_id: Mapped[str] = mapped_column(ForeignKey('countries.id'))
    division_id: Mapped[str | None] = mapped_column(String(160, collation='C'))
    latitude: Mapped[float] = mapped_column(Double)
    longitude: Mapped[float] = mapped_column(Double)
    population: Mapped[int] = mapped_column(BigInteger)
    feature_code: Mapped[str] = mapped_column(String(8))
    __table_args__ = (
        ForeignKeyConstraint(['division_id', 'country_id'], ['divisions.id', 'divisions.country_id']),
        CheckConstraint('population >= 0'),
        CheckConstraint('latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180'),
        Index('cities_country_id', 'country_id', 'id'),
        Index('cities_division_id', 'division_id', 'id'),
        Index('cities_population_id', 'population', 'id'),
        Index('cities_name_id', 'name', 'id'),
    )


class Airport(Named, Base):
    __tablename__ = 'airports'
    country_id: Mapped[str] = mapped_column(ForeignKey('countries.id'))
    region_id: Mapped[str | None] = mapped_column(String(160, collation='C'))
    city_id: Mapped[str | None] = mapped_column(ForeignKey('cities.id'))
    latitude: Mapped[float] = mapped_column(Double)
    longitude: Mapped[float] = mapped_column(Double)
    airport_type: Mapped[str] = mapped_column(String(24))
    scheduled_service: Mapped[bool] = mapped_column(Boolean)
    municipality: Mapped[str | None] = mapped_column(String(200))
    iata_code: Mapped[str | None] = mapped_column(String(3))
    __table_args__ = (
        ForeignKeyConstraint(['region_id', 'country_id'], ['divisions.id', 'divisions.country_id']),
        CheckConstraint('latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180'),
        Index('airports_country_id', 'country_id', 'id'),
        Index('airports_region_id', 'region_id', 'id'),
        Index('airports_name_id', 'name', 'id'),
    )
