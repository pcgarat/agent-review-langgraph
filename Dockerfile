FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# El CLI twg se usa como adaptador de Bitbucket y Jira.
# Su instalacion no es reproducible todavia: se copia el binario desde bin/ si
# esta presente (ver README) y, si no, el contenedor arranca sin el.
# Nunca se descarga desde una URL no verificada.
COPY bin/ /opt/twg/
RUN if [ -x /opt/twg/twg ]; then ln -sf /opt/twg/twg /usr/local/bin/twg; fi

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

CMD ["python", "-m", "reviews.worker.main"]
