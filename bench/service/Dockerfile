FROM python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --index-url https://pypi.org/simple -r requirements.lock && pip check
COPY directory ./directory
COPY fastapi_extension_msgspec ./fastapi_extension_msgspec
COPY tests ./tests
COPY pytest.ini .
CMD ["uvicorn", "directory.api:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "asyncio", "--http", "h11", "--workers", "1"]
