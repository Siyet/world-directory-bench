import asyncio
import os

from sqlalchemy.dialects.postgresql import insert

from .models import Base, Version
from .repository import Repository


async def initialize():
    repository = Repository(os.environ['DATABASE_URL'])
    try:
        async with repository.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(insert(Version).values(id=1, value=1).on_conflict_do_nothing())
    finally:
        await repository.close()


if __name__ == '__main__':
    asyncio.run(initialize())
