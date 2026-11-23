# Shadows the vendored October copy when placed before /work/service on PYTHONPATH,
# so the service sources stay byte-identical while the router comes from msgspec-fastapi main.
from msgspec_fastapi import MsgspecJSONResponse, MsgspecRouter, install_openapi

__all__ = ['MsgspecJSONResponse', 'MsgspecRouter', 'install_openapi']
