from __future__ import annotations

import os

import pytest

from agent_review.models_config import load_models_config
from agent_review.models_registry import ModelRegistry

pytestmark = pytest.mark.live

# Registro real del despliegue. El smoke comprueba contra la API lo que el YAML
# *declara*, porque `/v1/models` no expone capacidades: sin esto, una capacidad mal
# declarada (`json_schema` en un modelo que solo soporta `json_object`) se
# descubriria en produccion, con un 400 permanente que no se recupera.
CONFIG = os.environ.get("MODELS_CONFIG", "config/models.yaml")


def _registry() -> ModelRegistry:
    from agent_review.settings import get_settings

    return ModelRegistry.from_settings(get_settings())


@pytest.mark.skipif(
    not os.environ.get("LLM_API_KEY"), reason="define LLM_API_KEY para el smoke real"
)
def test_los_modelos_bindeados_existen_en_el_catalogo() -> None:
    """Todos los modelos bindeados aparecen en `GET /v1/models` del proveedor."""
    registry = _registry()
    problems = registry.probe()
    assert not problems, "\n".join(str(p) for p in problems)


@pytest.mark.skipif(
    not os.environ.get("LLM_API_KEY"), reason="define LLM_API_KEY para el smoke real"
)
def test_las_capacidades_declaradas_se_cumplen() -> None:
    """El YAML declara capacidades que la API no publica: se comprueban aqui.

    Para cada modelo bindeado se pide el JSON mode que declara y se exige una
    respuesta con contenido. Un `json_schema` no soportado da 400; un razonador con
    el presupuesto justo puede dar `content: null`, que es el fallo silencioso que
    el adaptador convierte en `LlmError`.
    """
    import httpx

    config = load_models_config(CONFIG)
    env = {name.upper(): str(getattr(_settings(), name)) for name in type(_settings()).model_fields}
    for model_name in dict.fromkeys(config.bindings.values()):
        model = config.models[model_name]
        provider = config.provider_of(model_name)
        key = env.get("LLM_API_KEY", "")
        response = httpx.post(
            f"{provider.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model_name,
                "messages": [{"role": "user", "content": 'Devuelve JSON valido {"ok": true}'}],
                "max_tokens": 256,
                **provider.defaults,
                **({"response_format": {"type": model.json_mode}} if model.json_mode else {}),
            },
            timeout=provider.timeout_s,
        )
        assert response.status_code == 200, (
            f"{model_name}: el proveedor rechazo el JSON mode declarado "
            f"({model.json_mode}): {response.text[:200]}"
        )
        data = response.json()
        content = data["choices"][0]["message"].get("content")
        assert content, (
            f"{model_name}: respuesta sin contenido (finish_reason="
            f"{data['choices'][0].get('finish_reason')}); revisa reasoning_effort/max_tokens"
        )


def _settings():  # noqa: ANN202
    from agent_review.settings import get_settings

    return get_settings()
