FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# `SANDBOX_RUNNER=true` construye la imagen del runner: ademas de git, necesita el
# CLI de Docker para hablar con su demonio rootless (los contenedores del PR que
# crea NO llevan el socket). La imagen del worker no lo lleva: menos superficie.
ARG SANDBOX_RUNNER=false
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && if [ "$SANDBOX_RUNNER" = "true" ]; then \
         apt-get install -y --no-install-recommends docker.io; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# El CLI twg se usa como adaptador de Bitbucket y Jira.
# Se copia el binario desde bin/ si esta presente. Nunca se descarga desde una
# URL no verificada. El worker valida `twg --version` en el arranque (A2): con
# REQUIRE_TWG=true, la imagen falla al construir si falta el binario, de modo que
# el fallo se ve en el build y no en la primera review.
ARG REQUIRE_TWG=false
COPY bin/ /opt/twg/
RUN if [ -x /opt/twg/twg ]; then ln -sf /opt/twg/twg /usr/local/bin/twg; fi \
    && if [ "$REQUIRE_TWG" = "true" ]; then twg --version; fi

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
# El contrato de hallazgo se valida en runtime (`domain/contracts.py`): sin esta
# copia, la primera review dentro del contenedor falla al validar un `Finding`.
COPY contracts ./contracts
RUN pip install --upgrade pip && pip install .

CMD ["python", "-m", "agent_review.worker.main"]
