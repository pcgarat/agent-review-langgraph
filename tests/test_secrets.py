"""Contrato de secretos declarado en el modelo (iniciativa 020, ADR-0022).

Dos guardas:

1. **Paridad.** `secrets_for` reproduce, escenario a escenario, el contrato que se
   escribia a mano antes del refactor. La referencia esta aqui congelada a proposito:
   si el refactor cambia un veredicto, el rojo senala la declaracion, no el contrato.
2. **Invariantes.** Todo campo con nombre de secreto declara su `Secret` (I1), y los
   nombres de proceso o condicion son cerrados (I3).
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from agent_review.settings import (
    _EXTRA_BY_PROCESS,
    _FEATURES,
    PLACEHOLDER_VALUES,
    PROCESSES,
    Secret,
    Settings,
    _secret_of,
    is_missing,
    secrets_for,
)

ROOT = Path(__file__).resolve().parents[1]


def _old_insecure_secrets(settings: Settings) -> list[str]:
    """El contrato del worker tal como se escribia a mano antes del refactor."""
    insecure: list[str] = []
    if is_missing(settings.tooling_webhook_secret):
        insecure.append("TOOLING_WEBHOOK_SECRET")
    if is_missing(settings.reviews_decision_token):
        insecure.append("REVIEWS_DECISION_TOKEN")
    if settings.vcs_provider == "github":
        if is_missing(settings.github_token):
            insecure.append("GITHUB_TOKEN")
    elif is_missing(settings.twg_bbc_token):
        insecure.append("TWG_BBC_TOKEN")
    if settings.teams_enabled and not settings.teams_notices_key.strip():
        insecure.append("TEAMS_NOTICES_KEY")
    if settings.llm_reviewer_enabled:
        if settings.models_config.strip():
            from agent_review.models_config import env_for, load_models_config, unresolved_secrets

            try:
                config = load_models_config(settings.models_config)
            except Exception:
                config = None
            if config is not None:
                insecure.extend(unresolved_secrets(config, env_for(settings)))
        elif is_missing(settings.llm_api_key):
            insecure.append("LLM_API_KEY")
    if settings.sandbox_enabled:
        if is_missing(settings.sandbox_runner_token):
            insecure.append("SANDBOX_RUNNER_TOKEN")
        if is_missing(settings.sandbox_git_token):
            insecure.append("SANDBOX_GIT_TOKEN")
    if settings.panel_enabled:
        insecure.extend(_old_panel_insecure_secrets(settings))
    if settings.review_engine == "claude":
        if not settings.claude_runner_url.strip():
            insecure.append("CLAUDE_RUNNER_URL")
        if is_missing(settings.claude_runner_token):
            insecure.append("CLAUDE_RUNNER_TOKEN")
    return insecure


def _old_panel_insecure_secrets(settings: Settings) -> list[str]:
    if is_missing(settings.panel_session_secret):
        return ["PANEL_SESSION_SECRET"]
    return []


def _old_fix_worker_insecure_secrets(settings: Settings) -> list[str]:
    insecure: list[str] = []
    if not settings.sandbox_runner_url.strip():
        insecure.append("SANDBOX_RUNNER_URL")
    if is_missing(settings.sandbox_runner_token):
        insecure.append("SANDBOX_RUNNER_TOKEN")
    return insecure


def _old_claude_runner_insecure_secrets(settings: Settings) -> list[str]:
    """El servicio que clona exige bearer y credencial de clonado, no el token del PR."""
    insecure: list[str] = []
    if is_missing(settings.claude_runner_token):
        insecure.append("CLAUDE_RUNNER_TOKEN")
    if is_missing(settings.claude_git_token):
        insecure.append("CLAUDE_GIT_TOKEN")
    return insecure


def _old_teams_bot_insecure_secrets(settings: Settings) -> list[str]:
    insecure: list[str] = []
    if not settings.teams_enabled:
        return insecure
    if is_missing(settings.teams_bot_id):
        insecure.append("TEAMS_BOT_ID")
    if is_missing(settings.teams_bot_tenant_id):
        insecure.append("TEAMS_BOT_TENANT_ID")
    if is_missing(settings.teams_bot_token):
        insecure.append("TEAMS_BOT_TOKEN")
    if is_missing(settings.teams_bot_shared_secret):
        insecure.append("TEAMS_BOT_SHARED_SECRET")
    return insecure


_OLD = {
    "api": _old_insecure_secrets,
    "worker": _old_insecure_secrets,
    "fix-worker": _old_fix_worker_insecure_secrets,
    "web": _old_panel_insecure_secrets,
    "teams-bot": _old_teams_bot_insecure_secrets,
    "claude-runner": _old_claude_runner_insecure_secrets,
}


def _scenarios() -> list[Settings]:
    """El espacio del contrato: VCS x sandbox x panel x teams x LLM (48 casos)."""
    scenarios: list[Settings] = []
    for vcs, sandbox, panel, teams, llm in itertools.product(
        ("twg", "github"),
        (False, True),
        (False, True),
        (False, True),
        ("off", "legacy", "registry"),
    ):
        overrides: dict[str, object] = {
            "environment": "production",
            "vcs_provider": vcs,
            "sandbox_enabled": sandbox,
            "panel_enabled": panel,
            "teams_enabled": teams,
        }
        if llm == "legacy":
            overrides["llm_reviewer_enabled"] = True
        elif llm == "registry":
            overrides["llm_reviewer_enabled"] = True
            overrides["models_config"] = str(ROOT / "config" / "models.yaml")
        scenarios.append(Settings(_env_file=None, **overrides))
    return scenarios


@pytest.mark.parametrize("process", sorted(PROCESSES))
def test_secrets_for_reproduce_el_contrato_anterior(process: str) -> None:
    """Paridad exacta con las funciones a mano: un veredicto distinto es un fallo
    del refactor, no una mejora."""
    reference = _OLD[process]
    for settings in _scenarios():
        expected = sorted(reference(settings))
        actual = sorted(secrets_for(settings, process))
        assert actual == expected, (
            f"{process}: el contrato cambio\n"
            f"  antes: {expected}\n"
            f"  ahora: {actual}\n"
            f"  settings: vcs={settings.vcs_provider} sandbox={settings.sandbox_enabled} "
            f"panel={settings.panel_enabled} teams={settings.teams_enabled} "
            f"llm={settings.llm_reviewer_enabled}/{settings.models_config}"
        )


def test_el_placeholder_tiene_una_sola_definicion() -> None:
    """Criterio 1: los cuatro sets literales y las comparaciones a pelo desaparecen
    del codigo; solo queda la lista de `settings.py`."""
    hits = []
    for path in (ROOT / "src").rglob("*.py"):
        if path.name == "settings.py":
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue  # los comentarios pueden citar el valor
            if '"changeme"' in stripped or '"change-me"' in stripped:
                hits.append(f"{path}: {stripped}")
    assert not hits, "quedan placeholders literales fuera de settings.py:\n" + "\n".join(hits)
    assert frozenset({"change-me", "changeme"}) == PLACEHOLDER_VALUES


def test_ningun_campo_secreto_sin_declaracion() -> None:
    """I1: un campo secreto nuevo sin `Secret` pone este test en rojo.

    El rojo puede aparecer en una rama que no toco nada del contrato: si sale de un
    commit anterior al ADR-0022 y anade un campo con nombre de secreto, su rama esta
    verde y la declaracion falta al integrar. Por eso el mensaje nombra el ADR y como
    declarar el campo, en vez de senalar solo el nombre.

    La excepcion real no necesita lista: `sandbox_runner_url` no termina en sufijo de
    secreto (es una URL) y por eso no lo mira este test; si algun campo futuro lo
    necesitara, su declaracion debe llevar `inventory_only` con el motivo.
    """
    undeclared = [
        name
        for name, field_info in Settings.model_fields.items()
        if (name.endswith(("_token", "_secret", "_password", "_api_key")))
        and _secret_of(field_info) is None
    ]
    assert undeclared == [], (
        f"campos secretos sin `Secret` en el metadata: {undeclared}.\n"
        "El contrato de secretos se deriva de `Settings` (ADR-0022, iniciativa 020):\n"
        "anade `Annotated[str, Secret(...)]` al campo, con `required_in` si solo lo\n"
        "exige algun proceso y `feature` si depende de una condicion (o\n"
        '`inventory_only="motivo"` si solo debe aparecer en el inventario).'
    )


def test_el_default_placeholder_obligue_a_declarar_el_campo() -> None:
    """Un default `change-me` es un secreto: tiene que estar declarado."""
    for name, field_info in Settings.model_fields.items():
        if isinstance(field_info.default, str) and field_info.default in PLACEHOLDER_VALUES:
            assert _secret_of(field_info) is not None, f"{name} lleva un placeholder sin declarar"


def test_procesos_y_features_son_cerrados() -> None:
    """I3: un nombre desconocido es un error al construir, no un secreto ignorado."""
    with pytest.raises(ValueError, match="procesos desconocidos"):
        Secret(required_in=frozenset({"api", "api2"}))
    with pytest.raises(ValueError, match="feature"):
        Secret(feature="inexistente")
    with pytest.raises(ValueError, match="expect"):
        Secret(expect="token")
    with pytest.raises(ValueError, match="inventory_only"):
        Secret(feature="teams", inventory_only="motivo")


def test_secrets_for_rechaza_un_proceso_desconocido() -> None:
    """Un `process` fuera de `PROCESSES` falla en vez de devolver contrato vacio.

    Sin esto, un punto de arranque con el nombre mal escrito (`fix_worker`) pasaria
    produccion con cero comprobaciones y los tests en verde."""
    settings = Settings(_env_file=None, environment="production")
    with pytest.raises(ValueError, match="proceso desconocido"):
        secrets_for(settings, "fix_worker")


def test_toda_feature_de_la_tabla_la_declara_alguien() -> None:
    """Sin andamiaje muerto: cada clave de `_FEATURES` la usa un campo o un contrato
    extra por proceso (`_EXTRA_BY_PROCESS`). Una clave huerfana se borra, no se deja."""
    declared = {
        secret.feature
        for field_info in Settings.model_fields.values()
        if (secret := _secret_of(field_info)) is not None and secret.feature is not None
    }
    used = declared | {feature for features in _EXTRA_BY_PROCESS.values() for feature in features}
    assert used == set(_FEATURES), f"features sin declarar: {sorted(set(_FEATURES) - used)}"


def test_los_secretos_de_peticion_se_quedan_fuera_del_contrato() -> None:
    """Q2: el bearer de consola del runner es guarda de peticion y no forma parte del
    arranque, aunque se declare en el inventario (I1). La firma del webhook y el bearer
    del runner **si** se exigen al arrancar; no son la excepcion."""
    console = _secret_of(Settings.model_fields["sandbox_console_token"])
    assert console is not None
    assert console.inventory_only
    settings = Settings(_env_file=None, environment="production", sandbox_enabled=True)
    for process in ("api", "worker", "fix-worker"):
        assert "SANDBOX_CONSOLE_TOKEN" not in secrets_for(settings, process)
