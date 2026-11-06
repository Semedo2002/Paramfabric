FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# gmsh's wheel links against X11/OpenGL client libraries even when used headless.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglu1-mesa libxcursor1 libxft2 libxinerama1 libxrender1 libxext6 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY paramfabric ./paramfabric
RUN pip install --no-cache-dir ".[distributed,s3]"
COPY scripts/entrypoint.sh /app/entrypoint.sh

RUN useradd --create-home --uid 10001 paramfabric \
    && mkdir -p /app/var \
    && chown -R paramfabric:paramfabric /app
USER 10001

EXPOSE 8000
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
CMD ["uvicorn", "paramfabric.server.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
