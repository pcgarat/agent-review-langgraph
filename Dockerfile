FROM python:3.12-slim AS app

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

# El paquete apt se llama claude-code; el ejecutable del PATH es `claude`.
# Esta etapa es la imagen de claude-runner y siempre lo instala: un runner sin
# el CLI no revisa. No es la etapa por defecto: el worker publica con otro
# token y no debe llevar el binario (ADR-0028).
FROM app AS claude-runner
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://downloads.claude.ai/keys/claude-code.asc \
         -o /etc/apt/keyrings/claude-code.asc \
    && fp="$(gpg --show-keys --with-colons /etc/apt/keyrings/claude-code.asc \
         | awk -F: '$1 == "fpr" { print $10; exit }')" \
    && test "$fp" = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE" \
    && printf '%s\n' \
         'deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main' \
         > /etc/apt/sources.list.d/claude-code.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends claude-code \
    && rm -rf /var/lib/apt/lists/* \
    && command -v claude \
    && claude --version

# `docker build` sin --target construye la ultima etapa. Tiene que ser la de
# la aplicacion, sin el CLI.
FROM app
