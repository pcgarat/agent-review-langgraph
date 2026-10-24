from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_review.api import main
from agent_review.api.main import app
from agent_review.domain.models import (
    ApprovalRequest,
    FindingAction,
    PendingDecision,
    ReviewRequest,
    Severity,
)
from agent_review.domain.pending import InMemoryPendingStore
from agent_review.idempotency import InMemoryIdempotencyStore
from agent_review.paused_store import InMemoryPausedStore
from agent_review.queue import InMemoryReviewQueue

client = TestClient(app)


@pytest.fixture(autouse=True)
def _artifacts_in_tmp(tmp_path: Path, monkeypatch) -> None:
    """Aisla el estado por proceso de la API entre tests.

    Los artefactos van a un directorio temporal (no al repo) y se fija la
    identidad del repo revisado que asume el payload; los casos que prueban otro
    proveedor lo sobrescriben a proposito. El `.env` y el entorno del host ya los
    neutraliza el `conftest.py` raiz.
    """
    settings = main._settings()
    monkeypatch.setattr(settings, "tooling_output_root", str(tmp_path))
    monkeypatch.setattr(settings, "bitbucket_workspace", "cqm")
    monkeypatch.setattr(settings, "bitbucket_repo", "emotivoo-v2")


def _reset_fallbacks(expected_tools: list[str] | None = None) -> None:
    main._fallback_store = InMemoryIdempotencyStore()
    main._fallback_queue = InMemoryReviewQueue()
    main._fallback_pending = InMemoryPendingStore(expected_tools=expected_tools or ["phpstan"])
    main._fallback_paused = InMemoryPausedStore()


def _payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "repo": "cqm/emotivoo-v2",
        "pr_id": 42,
        "head_sha": "abc123",
        "pipeline_id": "7",
        "tool": "phpstan",
        "payload": '{"files": {}}',
    }
    body.update(overrides)
    return body


def test_healthz() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_tooling_encola_cuando_la_barrera_se_completa() -> None:
    _reset_fallbacks()
    response = client.post("/webhook/tooling", json=_payload())
    assert response.status_code == 202
    assert response.json()["status"] == "aceptado"
    assert response.json()["key"] == "cqm/emotivoo-v2:42:abc123:7"
    assert main._fallback_queue is not None
    assert len(main._fallback_queue) == 1


def test_tooling_idempotente_por_artefacto_y_por_review() -> None:
    _reset_fallbacks()
    first = client.post("/webhook/tooling", json=_payload())
    assert first.status_code == 202
    assert first.json()["status"] == "aceptado"

    # Mismo artefacto otra vez: el evento de ingesta es duplicado, no se reencola.
    second = client.post("/webhook/tooling", json=_payload())
    assert second.status_code == 200
    assert second.json()["status"] == "duplicado"
    assert main._fallback_queue is not None
    assert len(main._fallback_queue) == 1


def test_tooling_barrera_de_dos_espera_al_segundo() -> None:
    _reset_fallbacks(expected_tools=["phpstan", "php-cs-fixer"])
    first = client.post("/webhook/tooling", json=_payload())
    assert first.status_code == 202
    assert first.json()["status"] == "pendiente"
    assert main._fallback_queue is not None
    assert len(main._fallback_queue) == 0

    second = client.post(
        "/webhook/tooling", json=_payload(tool="php-cs-fixer", payload='{"files": {}}')
    )
    assert second.status_code == 202
    assert second.json()["status"] == "aceptado"
    assert len(main._fallback_queue) == 1


def test_tooling_rechaza_payload_sin_campos() -> None:
    _reset_fallbacks()
    assert client.post("/webhook/tooling", json={"tool": "phpstan"}).status_code == 400
    assert client.post("/webhook/tooling", json=_payload(payload="   ")).status_code == 400
    assert client.post("/webhook/tooling", json=_payload(tool="")).status_code == 400


def test_tooling_firma_hmac_valida(monkeypatch) -> None:
    _reset_fallbacks()
    secret = "secreto"
    monkeypatch.setattr(main._settings(), "tooling_webhook_secret", secret)

    body = (
        b'{"repo": "cqm/emotivoo-v2", "pr_id": 1, "head_sha": "h", "tool": "phpstan", '
        b'"payload": "{}"}'
    )
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    ok = client.post(
        "/webhook/tooling",
        content=body,
        headers={"content-type": "application/json", "x-reviews-signature": signature},
    )
    assert ok.status_code == 202

    bad = client.post(
        "/webhook/tooling",
        content=body,
        headers={"content-type": "application/json", "x-reviews-signature": "malo"},
    )
    assert bad.status_code == 401


def test_tooling_firma_ausente_se_rechaza(monkeypatch) -> None:
    monkeypatch.setattr(main._settings(), "tooling_webhook_secret", "secreto")
    body = b'{"repo": "r", "pr_id": 1, "head_sha": "h", "tool": "phpstan", "payload": "{}"}'
    response = client.post(
        "/webhook/tooling",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 401


def test_webhook_de_pr_retirado() -> None:
    # La ingesta diferida sustituye al disparo por PR: el endpoint ya no existe.
    response = client.post("/webhook/bitbucket", json={"pullrequest": {"id": 1}})
    assert response.status_code == 404


def test_tooling_rechaza_payload_demasiado_grande(monkeypatch) -> None:
    _reset_fallbacks()
    monkeypatch.setattr(main._settings(), "max_tooling_payload_bytes", 16)
    response = client.post("/webhook/tooling", json=_payload())
    assert response.status_code == 413


def test_arranque_en_produccion_con_secreto_de_ejemplo_falla(monkeypatch) -> None:
    from agent_review.settings import require_production_secrets

    settings = main._settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "tooling_webhook_secret", "change-me")
    monkeypatch.setattr(settings, "twg_bbc_token", "token-real")
    with pytest.raises(RuntimeError, match="TOOLING_WEBHOOK_SECRET"):
        require_production_secrets(settings)


def test_arranque_en_produccion_sin_credencial_de_twg_falla(monkeypatch) -> None:
    from agent_review.settings import require_production_secrets

    settings = main._settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "tooling_webhook_secret", "secreto-real")
    monkeypatch.setattr(settings, "twg_bbc_token", "")
    with pytest.raises(RuntimeError, match="TWG_BBC_TOKEN"):
        require_production_secrets(settings)


@pytest.mark.parametrize(
    ("overrides", "secret"),
    [
        ({"twg_bbc_token": "change-me"}, "TWG_BBC_TOKEN"),
        ({"vcs_provider": "github", "github_token": "change-me"}, "GITHUB_TOKEN"),
        ({"sandbox_enabled": True, "sandbox_git_token": "change-me"}, "SANDBOX_GIT_TOKEN"),
        (
            {"llm_reviewer_enabled": True, "models_config": "", "llm_api_key": "change-me"},
            "LLM_API_KEY",
        ),
    ],
)
def test_el_placeholder_de_env_example_cuenta_como_sin_rellenar(
    monkeypatch, overrides: dict[str, object], secret: str
) -> None:
    """Copiar `.env.example` tal cual no puede pasar el contrato de produccion."""
    from agent_review.settings import require_production_secrets

    settings = main._settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "tooling_webhook_secret", "secreto-real")
    monkeypatch.setattr(settings, "reviews_decision_token", "token-decision-real")
    monkeypatch.setattr(settings, "twg_bbc_token", "token-real")
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)
    with pytest.raises(RuntimeError, match=secret):
        require_production_secrets(settings)


def test_env_example_declara_todas_las_variables_y_ningun_secreto_vacio() -> None:
    import re
    from pathlib import Path

    from agent_review.settings import Settings

    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    declared = re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", text, flags=re.M)
    missing = [name.upper() for name in Settings.model_fields if name.upper() not in dict(declared)]
    assert missing == [], f"faltan en .env.example: {missing}"
    # Vacio a proposito solo donde el vacio significa algo (desactivado/por defecto);
    # vacio en un secreto se colaria como «sin rellenar» solo por la via `is_missing`.
    intentionally_empty = {
        "SCOPE_IGNORE",
        "MAX_AUTONOMY_OVERRIDE",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "REVIEW_TOOLS_ROLES",  # 014: vacio = opt-in desactivado (una llamada por lote)
        "PANEL_CONFIG_LOCK",  # 027: vacio = sin candado, el operador publica el catalogo
    }
    empty = sorted(name for name, value in declared if not value.strip())
    assert empty, "el filtro de vacios no deberia ser vacuo: `.env.example` deja variables vacias"
    assert set(empty) <= intentionally_empty, f"sin placeholder: {empty}"


@pytest.mark.parametrize(
    ("settings_field", "overrides", "expected"),
    [
        ("twg_bbc_token", {}, "TWG_BBC_TOKEN"),
        ("github_token", {"vcs_provider": "github"}, "GITHUB_TOKEN"),
        ("tooling_webhook_secret", {}, "TOOLING_WEBHOOK_SECRET"),
        ("reviews_decision_token", {}, "REVIEWS_DECISION_TOKEN"),
        ("sandbox_git_token", {"sandbox_enabled": True}, "SANDBOX_GIT_TOKEN"),
        ("sandbox_runner_token", {"sandbox_enabled": True}, "SANDBOX_RUNNER_TOKEN"),
    ],
)
def test_una_variable_vacia_es_igual_de_insegura_que_el_placeholder(
    monkeypatch, settings_field: str, overrides: dict[str, object], expected: str
) -> None:
    """Vacio es el fallo mas probable (variable sin asignar); debe cerrar igual."""
    from agent_review.settings import require_production_secrets

    settings = main._settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "vcs_provider", "twg")
    monkeypatch.setattr(settings, "tooling_webhook_secret", "secreto-real")
    monkeypatch.setattr(settings, "reviews_decision_token", "token-decision-real")
    monkeypatch.setattr(settings, "twg_bbc_token", "token-real")
    monkeypatch.setattr(settings, "sandbox_runner_token", "token-runner-real")
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, settings_field, "")
    with pytest.raises(RuntimeError, match=expected):
        require_production_secrets(settings)


def test_arranque_en_produccion_con_teams_habilitado_sin_cola_falla(monkeypatch) -> None:
    from agent_review.settings import require_production_secrets

    settings = main._settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "tooling_webhook_secret", "secreto-real")
    monkeypatch.setattr(settings, "twg_bbc_token", "token-real")
    monkeypatch.setattr(settings, "teams_enabled", True)
    monkeypatch.setattr(settings, "teams_notices_key", "")
    with pytest.raises(RuntimeError, match="TEAMS_NOTICES_KEY"):
        require_production_secrets(settings)


def test_arranque_en_produccion_con_secreto_real_pasa(monkeypatch) -> None:
    from agent_review.settings import require_production_secrets

    settings = main._settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "tooling_webhook_secret", "secreto-real")
    monkeypatch.setattr(settings, "twg_bbc_token", "token-real")
    monkeypatch.setattr(settings, "teams_enabled", False)
    monkeypatch.setattr(settings, "reviews_decision_token", "token-decision-real")
    require_production_secrets(settings)


def test_arranque_en_produccion_sin_token_de_decision_falla(monkeypatch) -> None:
    from agent_review.settings import require_production_secrets

    settings = main._settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "tooling_webhook_secret", "secreto-real")
    monkeypatch.setattr(settings, "twg_bbc_token", "token-real")
    monkeypatch.setattr(settings, "reviews_decision_token", "change-me")
    with pytest.raises(RuntimeError, match="REVIEWS_DECISION_TOKEN"):
        require_production_secrets(settings)


def _secure_settings(monkeypatch) -> object:
    """Settings de produccion con todo el contrato base ya relleno."""
    settings = main._settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "tooling_webhook_secret", "secreto-real")
    monkeypatch.setattr(settings, "reviews_decision_token", "token-decision-real")
    monkeypatch.setattr(settings, "twg_bbc_token", "token-real")
    monkeypatch.setattr(settings, "teams_enabled", False)
    return settings


def test_arranque_con_sandbox_sin_token_de_runner_falla(monkeypatch) -> None:
    from agent_review.settings import require_production_secrets

    settings = _secure_settings(monkeypatch)
    monkeypatch.setattr(settings, "sandbox_enabled", True)
    monkeypatch.setattr(settings, "sandbox_runner_token", "change-me")
    monkeypatch.setattr(settings, "sandbox_git_token", "token-lectura-real")
    with pytest.raises(RuntimeError, match="SANDBOX_RUNNER_TOKEN"):
        require_production_secrets(settings)


def test_arranque_con_sandbox_sin_token_de_clonado_falla(monkeypatch) -> None:
    from agent_review.settings import require_production_secrets

    settings = _secure_settings(monkeypatch)
    monkeypatch.setattr(settings, "sandbox_enabled", True)
    monkeypatch.setattr(settings, "sandbox_runner_token", "token-runner-real")
    monkeypatch.setattr(settings, "sandbox_git_token", "")
    with pytest.raises(RuntimeError, match="SANDBOX_GIT_TOKEN"):
        require_production_secrets(settings)


def test_arranque_con_sandbox_desactivado_no_exige_sus_secretos(monkeypatch) -> None:
    from agent_review.settings import require_production_secrets

    settings = _secure_settings(monkeypatch)
    monkeypatch.setattr(settings, "sandbox_enabled", False)
    monkeypatch.setattr(settings, "sandbox_runner_token", "change-me")
    monkeypatch.setattr(settings, "sandbox_git_token", "")
    require_production_secrets(settings)


def _paused_request() -> ReviewRequest:
    return ReviewRequest(repo="cqm/emotivoo-v2", pr_id=42, head_sha="abc123", pipeline_id="7")


def _pause(review_key: str = "cqm/emotivoo-v2:42:abc123:7") -> None:
    request = _paused_request()
    approval = ApprovalRequest(
        review_key=review_key,
        repo=request.repo,
        pr_id=request.pr_id,
        head_sha=request.head_sha,
        pending=[
            PendingDecision(
                finding_id="f1",
                proposed_action=FindingAction.ESCALATE,
                reason="Tema protegido",
                matched_rule="default",
                policy_version="1",
                title="T",
                file="a.php",
                line=3,
                severity=Severity.P1,
            )
        ],
        command_hint=f"agent_review decide {review_key} --actor alice --approve f1",
    )
    assert main._fallback_paused is not None
    main._fallback_paused.pause(request, approval)


def test_list_paused_muestra_pendientes() -> None:
    _reset_fallbacks()
    _pause()
    response = client.get("/reviews/paused")
    assert response.status_code == 200
    paused = response.json()["paused"]
    assert [item["review_key"] for item in paused] == ["cqm/emotivoo-v2:42:abc123:7"]
    assert paused[0]["pending"][0]["finding_id"] == "f1"


def test_decision_encola_la_reanudacion(monkeypatch) -> None:
    _reset_fallbacks()
    monkeypatch.setattr(main._settings(), "reviews_decision_token", "change-me")
    _pause()
    response = client.post(
        "/reviews/cqm/emotivoo-v2:42:abc123:7/decision",
        json={"actor": "alice", "items": [{"finding_id": "f1", "outcome": "approved"}]},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "aceptado"
    assert main._fallback_queue is not None
    assert len(main._fallback_queue) == 1
    job = main._fallback_queue.dequeue()
    assert job is not None and job.decision is not None
    assert job.decision.actor == "alice"
    assert job.decision.review_key == "cqm/emotivoo-v2:42:abc123:7"


def test_decision_rechaza_review_no_pausada() -> None:
    _reset_fallbacks()
    response = client.post(
        "/reviews/no-existe/decision",
        json={"actor": "alice", "items": [{"finding_id": "f1", "outcome": "approved"}]},
    )
    assert response.status_code == 409


def test_decision_rechaza_finding_ajeno() -> None:
    _reset_fallbacks()
    _pause()
    response = client.post(
        "/reviews/cqm/emotivoo-v2:42:abc123:7/decision",
        json={"actor": "alice", "items": [{"finding_id": "otro", "outcome": "approved"}]},
    )
    assert response.status_code == 409
    assert main._fallback_queue is not None and len(main._fallback_queue) == 0


def test_decision_rechaza_sin_actor() -> None:
    _reset_fallbacks()
    _pause()
    response = client.post(
        "/reviews/cqm/emotivoo-v2:42:abc123:7/decision",
        json={"actor": "  ", "items": [{"finding_id": "f1", "outcome": "approved"}]},
    )
    assert response.status_code == 409


def test_decision_exige_bearer_cuando_hay_token(monkeypatch) -> None:
    _reset_fallbacks()
    monkeypatch.setattr(main._settings(), "reviews_decision_token", "secreto-decision")
    _pause()
    body = {"actor": "alice", "items": [{"finding_id": "f1", "outcome": "approved"}]}
    url = "/reviews/cqm/emotivoo-v2:42:abc123:7/decision"

    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers={"Authorization": "Bearer malo"}).status_code == 401
    ok = client.post(url, json=body, headers={"Authorization": "Bearer secreto-decision"})
    assert ok.status_code == 202


def test_estado_de_review_pausada() -> None:
    _reset_fallbacks()
    _pause()
    response = client.get("/reviews/cqm/emotivoo-v2:42:abc123:7")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "PAUSED"
    assert body["pending"][0]["finding_id"] == "f1"


def test_estado_de_review_desconocida_es_404() -> None:
    _reset_fallbacks()
    assert client.get("/reviews/cqm/emotivoo-v2:42:abc123:7").status_code == 404


def test_reviews_paused_no_lo_traga_la_ruta_comodin() -> None:
    # `/reviews/paused` y `/reviews/{review_key:path}` conviven: el literal debe
    # seguir ganando para no confundir el listado con una review llamada "paused".
    _reset_fallbacks()
    _pause()
    response = client.get("/reviews/paused")
    assert response.status_code == 200
    assert "paused" in response.json()


def test_b7_fallo_al_persistir_libera_la_clave_de_ingesta(monkeypatch) -> None:
    # Antes la clave quedaba reclamada 7 dias y el reintento del CI era `duplicado`.
    _reset_fallbacks()
    real_persist = main._persist_artifact
    calls = {"n": 0}

    def _flaky_persist(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disco lleno")
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(main, "_persist_artifact", _flaky_persist)
    failing = TestClient(app, raise_server_exceptions=False)

    assert failing.post("/webhook/tooling", json=_payload()).status_code == 500
    retry = failing.post("/webhook/tooling", json=_payload())

    assert retry.status_code == 202
    assert retry.json()["status"] == "aceptado"
    assert main._fallback_queue is not None and len(main._fallback_queue) == 1


def test_b7_fallo_al_encolar_libera_la_clave_de_review(monkeypatch) -> None:
    _reset_fallbacks()
    assert main._fallback_queue is not None
    real_enqueue = main._fallback_queue.enqueue
    calls = {"n": 0}

    def _flaky_enqueue(request: ReviewRequest) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("redis caido")
        real_enqueue(request)

    monkeypatch.setattr(main._fallback_queue, "enqueue", _flaky_enqueue)
    failing = TestClient(app, raise_server_exceptions=False)

    assert failing.post("/webhook/tooling", json=_payload()).status_code == 500
    retry = failing.post("/webhook/tooling", json=_payload())

    assert retry.json()["status"] == "aceptado", "el reintento del CI dispara la review"
    assert len(main._fallback_queue) == 1


def test_b6_rechaza_un_repo_distinto_del_configurado(monkeypatch) -> None:
    # El adaptador VCS ignora el repo del job: aceptar otro repo revisaria (y
    # comentaria) el PR con el mismo numero del repo configurado.
    _reset_fallbacks()
    monkeypatch.setattr(main._settings(), "bitbucket_workspace", "cqm")
    monkeypatch.setattr(main._settings(), "bitbucket_repo", "emotivoo-v2")

    other = client.post("/webhook/tooling", json=_payload(repo="cqm/otro-repo"))
    assert other.status_code == 400
    assert main._fallback_queue is not None and len(main._fallback_queue) == 0

    same = client.post("/webhook/tooling", json=_payload(repo="cqm/emotivoo-v2"))
    assert same.status_code == 202


def test_verify_repo_usa_la_identidad_del_proveedor_activo(monkeypatch) -> None:
    from agent_review.api.main import _expected_repo

    settings = main._settings()
    monkeypatch.setattr(settings, "vcs_provider", "github")
    monkeypatch.setattr(settings, "github_owner", "cqm")
    monkeypatch.setattr(settings, "github_repo", "reviews")
    assert _expected_repo(settings) == "cqm/reviews"

    monkeypatch.setattr(settings, "vcs_provider", "twg")
    monkeypatch.setattr(settings, "bitbucket_workspace", "cqm")
    monkeypatch.setattr(settings, "bitbucket_repo", "emotivoo-v2")
    assert _expected_repo(settings) == "cqm/emotivoo-v2"


def test_vcs_provider_desconocido_falla_al_construir_settings() -> None:
    from agent_review.settings import Settings

    with pytest.raises(ValueError, match="VCS_PROVIDER"):
        Settings(vcs_provider="gitlab")


def test_arranque_en_produccion_con_github_exige_github_token(monkeypatch) -> None:
    from agent_review.settings import require_production_secrets

    settings = main._settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "tooling_webhook_secret", "secreto-real")
    monkeypatch.setattr(settings, "reviews_decision_token", "secreto-decision")
    monkeypatch.setattr(settings, "vcs_provider", "github")
    monkeypatch.setattr(settings, "github_token", "")
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        require_production_secrets(settings)
