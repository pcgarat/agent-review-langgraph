from __future__ import annotations

from pathlib import Path

import httpx

from agent_review.models_registry import ModelRegistry
from agent_review.settings import Settings

_CONFIG = """\
version: 1
providers:
  nan:
    base_url: https://api.nan.builders/v1
    api_key: ${LLM_API_KEY}
    timeout_s: 30
    defaults:
      reasoning_effort: none
models:
  modelo-a: {provider: nan, kind: chat, json_mode: json_object}
  modelo-b: {provider: nan, kind: chat, json_mode: json_schema}
  modelo-c: {provider: nan, kind: chat}
bindings:
  correctness: modelo-a
  security: modelo-a
  fixer: modelo-b
"""


def _settings(tmp_path: Path, text: str = _CONFIG, **overrides) -> Settings:
    path = tmp_path / "models.yaml"
    path.write_text(text, encoding="utf-8")
    base = {
        "llm_reviewer_enabled": True,
        "models_config": str(path),
        "llm_api_key": "sk-test",
    }
    base.update(overrides)
    return Settings(**base)


def test_cada_rol_resuelve_su_modelo(tmp_path: Path) -> None:
    registry = ModelRegistry.from_settings(_settings(tmp_path))
    assert registry.model_for("correctness") == "modelo-a"
    assert registry.model_for("fixer") == "modelo-b"
    # El rol sin binding no tiene puerto (la persona no se construye).
    assert registry.model_for("arch") is None
    assert registry.port_for("arch") is None


def test_roles_con_el_mismo_modelo_comparten_puerto(tmp_path: Path) -> None:
    registry = ModelRegistry.from_settings(_settings(tmp_path))
    # `correctness` y `security` apuntan al mismo modelo: una sola instancia.
    assert registry.port_for("correctness") is registry.port_for("security")
    assert registry.port_for("correctness") is not registry.port_for("fixer")


def test_desactivado_no_hay_registro(tmp_path: Path) -> None:
    registry = ModelRegistry.from_settings(_settings(tmp_path, llm_reviewer_enabled=False))
    assert registry.port_for("correctness") is None
    assert registry.config.models == {}


def test_probe_detecta_un_modelo_inexistente(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(200, json={"data": [{"id": "modelo-a"}, {"id": "otro"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    registry = ModelRegistry.from_settings(_settings(tmp_path))
    problems = registry.probe(client=client)
    # `modelo-b` (el del fixer) no esta en el catalogo: el fallo que hoy se
    # descubriria en la primera review, no en el arranque.
    assert len(problems) == 1
    assert "modelo-b" in str(problems[0])


def test_probe_sin_problemas_si_todo_esta(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "modelo-a"}, {"id": "modelo-b"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    registry = ModelRegistry.from_settings(_settings(tmp_path))
    assert registry.probe(client=client) == []


def test_probe_soporta_un_proveedor_caido(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    registry = ModelRegistry.from_settings(_settings(tmp_path))
    problems = registry.probe(client=client)
    assert len(problems) == 1
    assert "no se pudo consultar" in str(problems[0])
