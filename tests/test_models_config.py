from __future__ import annotations

from pathlib import Path

import pytest

from agent_review.models_config import (
    ModelsConfigError,
    implicit_config,
    load_models_config,
    resolve_reference,
    unresolved_secrets,
)
from agent_review.settings import Settings

_VALID = """\
version: 1
providers:
  nan:
    protocol: openai
    base_url: https://api.nan.builders/v1
    api_key: ${LLM_API_KEY}
    timeout_s: 42
    defaults:
      temperature: 0
      reasoning_effort: none
models:
  modelo-a:
    provider: nan
    kind: chat
    context_window: 1048576
    json_mode: json_object
  modelo-b:
    provider: nan
    kind: chat
    json_mode: json_schema
bindings:
  correctness: modelo-a
  fixer: modelo-b
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_registro_valido_se_carga(tmp_path: Path) -> None:
    config = load_models_config(_write(tmp_path, _VALID))
    assert config.bindings == {"correctness": "modelo-a", "fixer": "modelo-b"}
    assert config.models["modelo-a"].context_window == 1048576
    assert config.models["modelo-a"].json_mode == "json_object"
    assert config.models["modelo-b"].json_mode == "json_schema"
    assert config.providers["nan"].timeout_s == 42
    assert config.providers["nan"].defaults["reasoning_effort"] == "none"


def test_binding_a_un_modelo_inexistente_falla(tmp_path: Path) -> None:
    text = _VALID.replace("correctness: modelo-a", "correctness: no-existe")
    with pytest.raises(ModelsConfigError, match="no esta declarado"):
        load_models_config(_write(tmp_path, text))


def test_rol_desconocido_falla(tmp_path: Path) -> None:
    # Un typo (`fixerr`) debe fallar al arrancar, no degradar a "sin persona" en
    # silencio: es exactamente el fallo que el registro existe para evitar.
    text = _VALID.replace("fixer: modelo-b", "fixerr: modelo-b")
    with pytest.raises(ModelsConfigError, match="rol desconocido"):
        load_models_config(_write(tmp_path, text))


def test_binding_a_un_modelo_no_chat_falla(tmp_path: Path) -> None:
    text = _VALID.replace(
        "  modelo-b:\n    provider: nan\n    kind: chat",
        "  modelo-b:\n    provider: nan\n    kind: embedding",
    )
    with pytest.raises(ModelsConfigError, match="y no 'chat'"):
        load_models_config(_write(tmp_path, text))


def test_modelo_con_proveedor_no_declarado_falla(tmp_path: Path) -> None:
    text = _VALID.replace("provider: nan", "provider: otro")
    with pytest.raises(ModelsConfigError, match="proveedor 'otro'"):
        load_models_config(_write(tmp_path, text))


def test_clave_desconocida_falla(tmp_path: Path) -> None:
    text = _VALID.replace("    context_window: 1048576", "    contxt_window: 1048576")
    with pytest.raises(ModelsConfigError):
        load_models_config(_write(tmp_path, text))


def test_fichero_inexistente_es_error_de_arranque(tmp_path: Path) -> None:
    with pytest.raises(ModelsConfigError, match="no se pudo leer"):
        load_models_config(tmp_path / "no-existe.yaml")


def test_yaml_invalido_es_error_de_arranque(tmp_path: Path) -> None:
    with pytest.raises(ModelsConfigError, match="no es YAML valido"):
        load_models_config(_write(tmp_path, "version: [\n"))


def test_referencia_resuelve_desde_el_entorno() -> None:
    assert resolve_reference("${LLM_API_KEY}", {"LLM_API_KEY": "sk-1"}) == "sk-1"
    # Un valor literal no es una referencia: se devuelve tal cual.
    assert resolve_reference("literal", {"NAN_API_KEY": "sk-1"}) == "literal"
    # Una referencia sin resolver es cadena vacia, nunca el literal `${VAR}`
    # viajando al proveedor.
    assert resolve_reference("${LLM_API_KEY}", {}) == ""


def test_secreto_sin_resolver_se_nombra_sin_valor(tmp_path: Path) -> None:
    config = load_models_config(_write(tmp_path, _VALID))
    missing = unresolved_secrets(config, env={})
    assert len(missing) == 1
    assert "LLM_API_KEY" in missing[0]
    assert unresolved_secrets(config, env={"LLM_API_KEY": "sk-1"}) == []


def test_registro_implicito_sin_registro_declarativo() -> None:
    settings = Settings(
        llm_reviewer_enabled=True,
        models_config="",
        llm_model="gpt-x",
        llm_base_url="https://example.test/v1",
        llm_api_key="k",
    )
    config = implicit_config(settings)
    assert config.bindings == {"correctness": "gpt-x", "fixer": "gpt-x"}
    assert config.models["gpt-x"].provider == "openai-compatible"
    # El proveedor implicito referencia `LLM_API_KEY`, no un valor literal.
    assert config.providers["openai-compatible"].api_key == "${LLM_API_KEY}"
    assert unresolved_secrets(config, env={"LLM_API_KEY": "k"}) == []


def test_registro_implicito_sin_modelo_es_vacio() -> None:
    # Sin `LLM_MODEL` no hay nada que bindear: la persona queda desactivada, que
    # es el comportamiento de hoy.
    config = implicit_config(Settings(models_config="", llm_model=""))
    assert config.bindings == {}
    assert config.models == {}
