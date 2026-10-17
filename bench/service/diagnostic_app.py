import gc
import os
if os.environ.get("DB_BACKEND") == "asyncpg":
    from raw_app import app
else:
    from directory.api import app
@app.get("/__bench_gc", include_in_schema=False)
async def gc_snapshot():
    return {"counts": gc.get_count(), "stats": gc.get_stats(), "enabled": gc.isenabled()}
