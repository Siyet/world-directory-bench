"""City GET benchmark entrypoint; other inherited operations are unsupported."""
from directory import api
from asyncpg_repository import Repository

api.Repository = Repository
app = api.app
