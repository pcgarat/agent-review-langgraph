from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from agent_review.adapters.openai_llm import OpenAiCompatibleLlm
from agent_review.models_config import (
    ModelsConfig,
    ProviderSpec,
    env_for,
    implicit_config,
    load_models_config,
    resolved_api_key,
)
from agent_review.ports.llm import LlmPort
from agent_review.settings import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Problem:
    """Un problema detectado en el registro. No es una excepcion: se acumulan."""

    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


class ModelRegistry:
    """Bindings declarativos (rol -> modelo) resueltos a puertos.

    Se construye **una vez, al arrancar** (D2): no hay recarga en caliente. El
    codigo que construye personas y fixer pide `port_for(rol)` y nunca conoce un
    nombre de modelo.
    """

    def __init__(self, config: ModelsConfig, *, env: dict[str, str]) -> None:
        self._config = config
        self._env = env
        # Cache por modelo: dos roles con el mismo modelo comparten instancia, de
        # modo que el `last_estimated_tokens` de un puerto no se duplica sin motivo.
        self._ports: dict[str, LlmPort] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> ModelRegistry:
        """Registro del despliegue: el YAML si lo hay, o el implicito de `LLM_*`.

        Respeta la puerta que ya existe: sin `LLM_REVIEWER_ENABLED` no hay registro,
        ni siquiera si hay un YAML (la persona LLM sigue siendo opt-in).
        """
        if not settings.llm_reviewer_enabled:
            return cls(ModelsConfig(), env=env_for(settings))
        if settings.models_config.strip():
            config = load_models_config(settings.models_config)
        else:
            config = implicit_config(settings)
        return cls(config, env=env_for(settings))

    @property
    def config(self) -> ModelsConfig:
        return self._config

    def model_for(self, role: str) -> str | None:
        """Nombre del modelo bindeado al rol, o `None` si no hay binding."""
        return self._config.bindings.get(role)

    def port_for(self, role: str) -> LlmPort | None:
        """Puerto del modelo bindeado al rol, o `None` si el rol no esta bindeado."""
        model_name = self.model_for(role)
        if model_name is None:
            return None
        if model_name not in self._ports:
            self._ports[model_name] = self._build(model_name)
        return self._ports[model_name]

    def _build(self, model_name: str) -> LlmPort:
        model = self._config.models[model_name]
        provider = self._config.provider_of(model_name)
        return OpenAiCompatibleLlm(
            api_key=resolved_api_key(provider, self._env),
            model=model_name,
            base_url=provider.base_url,
            timeout_s=provider.timeout_s,
            defaults=dict(provider.defaults),
            json_mode=model.json_mode,
            reasoning_min_max_tokens=model.reasoning_min_max_tokens,
        )

    def probe(self, *, client: Any | None = None) -> list[Problem]:
        """Contrasta los modelos bindeados contra `GET /v1/models`.

        Detecta el fallo que hoy no se ve hasta la primera review: un `LLM_MODEL`
        con typo o retirado por el proveedor. Un problema no es fatal por si mismo
        (el endpoint puede estar caido); `validate_models` decide cual lo es.
        Se consulta una vez por proveedor, no una vez por modelo.
        """
        problems: list[Problem] = []
        for provider_name in dict.fromkeys(
            self._config.models[name].provider for name in self._config.bindings.values()
        ):
            provider = self._config.providers[provider_name]
            listed = self._list_models(provider, client=client)
            if listed is None:
                problems.append(
                    Problem(provider_name, f"no se pudo consultar {provider.base_url}/models")
                )
                continue
            for model_name in dict.fromkeys(self._config.bindings.values()):
                if self._config.models[model_name].provider != provider_name:
                    continue
                if model_name not in listed:
                    problems.append(
                        Problem(
                            provider_name,
                            f"el modelo {model_name!r} no aparece en {provider.base_url}/models",
                        )
                    )
        return problems

    def _list_models(self, provider: ProviderSpec, *, client: Any | None) -> set[str] | None:
        import httpx

        url = f"{provider.base_url.rstrip('/')}/models"
        headers = {"Authorization": f"Bearer {resolved_api_key(provider, self._env)}"}
        try:
            if client is not None:
                response = client.get(url, headers=headers, timeout=provider.timeout_s)
            else:
                response = httpx.get(url, headers=headers, timeout=provider.timeout_s)
            response.raise_for_status()
            data = response.json()
            entries = data.get("data") if isinstance(data, dict) else None
            if not isinstance(entries, list):
                return None
            return {str(item.get("id")) for item in entries if isinstance(item, dict)}
        except Exception as exc:
            logger.warning("No se pudo consultar %s: %s", url, exc)
            return None
