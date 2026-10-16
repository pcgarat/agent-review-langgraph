from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_review.settings import Settings

# `kind` de un modelo declarado. Solo `chat` es utilizable hoy; el resto se declara
# para poder *detectarlo* y fallar con un mensaje claro, en vez de descubrir en la
# primera review que un binding apunta a un modelo de embeddings.
ModelKind = Literal["chat", "embedding", "rerank", "tts", "stt"]
JsonMode = Literal["json_object", "json_schema"]

# Roles que el worker puede bindear. Es un conjunto cerrado a proposito: un typo
# (`fixerr`) no debe degradar a "sin persona" en silencio, sino fallar al arrancar.
ROLES = ("correctness", "security", "arch", "fixer")

# `${VAR}`: la unica forma de referenciar un secreto. El valor nunca esta en el YAML.
_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class ModelsConfigError(ValueError):
    """El registro declarativo no es valido. Falla el arranque, no la review."""


class ProviderSpec(BaseModel):
    """Proveedor LLM. `protocol` es la puerta a otros formatos; hoy solo openai."""

    model_config = ConfigDict(extra="forbid")

    protocol: Literal["openai"] = "openai"
    base_url: str
    # Referencia (`${VAR}`) o valor literal. Se resuelve contra `Settings`.
    api_key: str = ""
    timeout_s: float = 60.0
    # Se fusiona con cada modelo del proveedor si el modelo no lo redefine.
    defaults: dict[str, Any] = Field(default_factory=dict)


class ModelSpec(BaseModel):
    """Un modelo concreto y lo que *sabe hacer*.

    Las capacidades se declaran aqui porque `/v1/models` no las expone: un
    `json_schema` pedido a un modelo que no lo soporta es un 400 **permanente**
    (la `RetryPolicy` no lo recupera y la persona degrada a 0 hallazgos).
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    kind: ModelKind = "chat"
    context_window: int | None = None
    json_mode: JsonMode | None = None
    # Con razonamiento activo la API puede subir `max_tokens` por su cuenta: si se
    # declara, la estimacion de coste deja de mentir.
    reasoning_min_max_tokens: int | None = None


class ModelsConfig(BaseModel):
    """Registro completo: proveedores, modelos y bindings de rol."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    providers: dict[str, ProviderSpec] = Field(default_factory=dict)
    models: dict[str, ModelSpec] = Field(default_factory=dict)
    bindings: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_contract(self) -> ModelsConfig:
        for name, model in self.models.items():
            if model.provider not in self.providers:
                raise ValueError(
                    f"el modelo {name!r} referencia el proveedor {model.provider!r}, "
                    f"que no esta declarado"
                )
        for role, model_name in self.bindings.items():
            if role not in ROLES:
                raise ValueError(f"rol desconocido {role!r}; los validos son {sorted(ROLES)}")
            if model_name not in self.models:
                raise ValueError(
                    f"el rol {role!r} apunta al modelo {model_name!r}, que no esta declarado"
                )
            if self.models[model_name].kind != "chat":
                raise ValueError(
                    f"el rol {role!r} apunta al modelo {model_name!r}, que es "
                    f"{self.models[model_name].kind!r} y no 'chat'"
                )
        return self

    def provider_of(self, model_name: str) -> ProviderSpec:
        return self.providers[self.models[model_name].provider]


def resolve_reference(value: str, env: Mapping[str, str]) -> str:
    """Resuelve una referencia `${VAR}` contra el entorno.

    Un valor que no es una referencia se devuelve tal cual. Una referencia sin
    resolver devuelve cadena vacia: quien la consume la trata como secreto ausente,
    nunca como el literal `"${VAR}"` viajando al proveedor.
    """
    match = _REFERENCE.match(value.strip())
    if match is None:
        return value
    return str(env.get(match.group(1), "")).strip()


def env_for(settings: Settings) -> dict[str, str]:
    """Entorno con el que se resuelven las referencias del YAML.

    Se limita a los campos de `Settings` (que ya incorporan el `.env`): el YAML no
    puede leer un secreto arbitrario del proceso, solo los declarados como ajuste
    del agente. Las claves van en mayusculas, como las variables de entorno: la
    referencia `${LLM_API_KEY}` del YAML resuelve al campo `llm_api_key`. Anadir un
    proveedor con un secreto nuevo es anadir su campo, igual que con
    `TWG_BBC_TOKEN` o `GITHUB_TOKEN` (ADR-0013).
    """
    return {name.upper(): str(getattr(settings, name)) for name in type(settings).model_fields}


def load_models_config(path: str | Path) -> ModelsConfig:
    """Lee y valida el registro.

    Lanza `ModelsConfigError` si no existe, no es un mapa o no cumple el contrato:
    un registro mal escrito es un error de arranque, no una degradacion.
    """
    file = Path(path)
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ModelsConfigError(f"no se pudo leer el registro de modelos en {file}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ModelsConfigError(
            f"el registro de modelos en {file} no es YAML valido: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ModelsConfigError(f"el registro de modelos en {file} debe ser un mapa")
    try:
        return ModelsConfig.model_validate(raw)
    except Exception as exc:
        raise ModelsConfigError(
            f"el registro de modelos en {file} no cumple el contrato: {exc}"
        ) from exc


def implicit_config(settings: Settings) -> ModelsConfig:
    """Registro de un solo modelo a partir de `LLM_*` (compatibilidad, D7).

    Un despliegue sin registro declarativo sigue funcionando igual que hoy: un
    proveedor compatible-OpenAI y el modelo que diga `LLM_MODEL`. Solo se bindean
    los roles que la via implicita cubria antes (`correctness` y `fixer`): crear de
    golpe personas `security`/`arch` cambiaria el fan-out de un despliegue
    existente. Sin `LLM_MODEL` no hay bindings, que es la persona desactivada.
    """
    provider = "openai-compatible"
    if not settings.llm_model.strip():
        return ModelsConfig()
    return ModelsConfig(
        providers={
            provider: ProviderSpec(
                protocol="openai",
                base_url=settings.llm_base_url or "https://api.openai.com/v1",
                api_key="${LLM_API_KEY}",
            )
        },
        models={settings.llm_model: ModelSpec(provider=provider, kind="chat")},
        bindings={"correctness": settings.llm_model, "fixer": settings.llm_model},
    )


def unresolved_secrets(config: ModelsConfig, env: Mapping[str, str]) -> list[str]:
    """Referencias de secreto sin resolver. Se integra en `insecure_secrets()`.

    Nombra la variable, nunca el valor: es lo que hace falta para arreglarlo.
    """
    missing: list[str] = []
    for name, provider in config.providers.items():
        reference = _REFERENCE.match(provider.api_key.strip())
        if reference is not None and not str(env.get(reference.group(1), "")).strip():
            missing.append(f"MODELS_API_KEY de {name} ({reference.group(1)})")
    return missing


def resolved_api_key(provider: ProviderSpec, env: Mapping[str, str]) -> str:
    """Clave del proveedor ya resuelta (cadena vacia si la referencia falta)."""
    return resolve_reference(provider.api_key, env)
