"""Contrato de los diales del panel (026)."""

from __future__ import annotations

import threading

import pytest

from agent_review.config_store import ConfigReadError, MemoryConfigStore, RememberingStore
from agent_review.panel_config import (
    ConfigError,
    apply_document,
    catalog,
    choose_document,
    document_for,
    initial_values,
    lock_names,
)
from agent_review.settings import Settings

_CATALOG = {
    "llm_max_findings",
    "llm_budget_tokens_per_pr",
    "llm_batch_max_chars",
    "llm_batch_retries",
    "llm_context_max_chars",
    "verifier_max_findings",
    "verifier_max_tool_calls",
    "verifier_max_context_bytes",
    "verifier_require_verified_for",
    "review_tools_roles",
    "review_tools_max_turns",
    "review_tools_max_calls",
    "review_tools_max_bytes",
    "review_tools_calls_per_turn",
    "review_tools_grep_mode",
    "review_tools_read_outline",
    "review_tools_read_state",
    "review_tools_truncation_notice",
    "review_tools_strict_calls",
    "review_tools_precache_context",
    "review_tools_io_cache",
    "max_autonomy_override",
    "scope_ignore",
}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def _form(settings: Settings, **changes: object) -> dict[str, object]:
    form = initial_values(settings)
    form.update(changes)
    return form


def test_el_catalogo_es_el_de_la_spec_y_no_incluye_secretos() -> None:
    names = {item.name for item in catalog()}
    assert names == _CATALOG
    for item in catalog():
        assert item.help
        assert item.section


def test_sin_documento_el_valor_efectivo_es_el_de_settings() -> None:
    settings = _settings(llm_max_findings=7)
    assert apply_document(settings, {}) is settings
    assert initial_values(settings)["llm_max_findings"] == 7


def test_el_documento_solo_guarda_lo_que_cambia() -> None:
    settings = _settings()
    document = document_for(
        settings, _form(settings, llm_max_findings=3), autonomy_ceiling="report"
    )
    assert document == {"llm_max_findings": 3}
    assert apply_document(settings, document).llm_max_findings == 3


def test_restaurar_quita_la_clave() -> None:
    settings = _settings()
    restored = document_for(settings, _form(settings), autonomy_ceiling="report")
    assert restored == {}


def test_rechaza_clave_ajena_entero_negativo_y_modo_de_grep() -> None:
    settings = _settings()
    with pytest.raises(ConfigError) as unknown:
        document_for(
            settings,
            {**_form(settings), "github_token": "x"},
            autonomy_ceiling="report",
        )
    assert unknown.value.field == "github_token"
    with pytest.raises(ConfigError) as negative:
        document_for(settings, _form(settings, llm_batch_retries=-1), autonomy_ceiling="report")
    assert negative.value.field == "llm_batch_retries"
    with pytest.raises(ConfigError) as mode:
        document_for(
            settings, _form(settings, review_tools_grep_mode="glob"), autonomy_ceiling="report"
        )
    assert mode.value.field == "review_tools_grep_mode"


def test_el_techo_de_autonomia_no_puede_subir() -> None:
    settings = _settings()
    with pytest.raises(ConfigError) as raised:
        document_for(
            settings,
            _form(settings, max_autonomy_override="fix_auto"),
            autonomy_ceiling="report",
        )
    assert raised.value.field == "max_autonomy_override"
    kept = document_for(
        settings,
        _form(settings, max_autonomy_override=""),
        autonomy_ceiling="report",
    )
    assert kept == {}


def test_un_candado_desconocido_aborta_y_uno_conocido_no_deja_cambiar() -> None:
    with pytest.raises(ConfigError):
        lock_names(_settings(panel_config_lock="redis_url"))
    settings = _settings(panel_config_lock="llm_max_findings")
    with pytest.raises(ConfigError) as locked:
        document_for(settings, _form(settings, llm_max_findings=1), autonomy_ceiling="report")
    assert locked.value.field == "llm_max_findings"
    assert apply_document(settings, {"llm_max_findings": 1}).llm_max_findings == 10


def test_prepare_review_usa_lo_publicado_y_el_reintento_no_hereda_el_hilo() -> None:
    from types import SimpleNamespace

    from agent_review.config_store import MemoryConfigStore
    from agent_review.domain.models import ReviewRequest
    from agent_review.worker.main import prepare_review, process_request

    store = MemoryConfigStore()
    store.publish(expected=0, overrides={"llm_max_findings": 2}, actor="ana")
    settings = _settings()
    request = ReviewRequest(repo="r", pr_id=1, head_sha="a")
    effective, snapshot = prepare_review(settings, store, None, request)
    assert effective.llm_max_findings == 2
    assert snapshot == {"revision": 1, "overrides": {"llm_max_findings": 2}}

    class _Saver:
        def get_tuple(self, config: object) -> object:
            del config
            return SimpleNamespace(
                checkpoint={
                    "channel_values": {
                        "dial_snapshot": {"revision": 4, "overrides": {"llm_max_findings": 8}}
                    }
                }
            )

    kept, old = prepare_review(settings, store, _Saver(), request)
    assert kept.llm_max_findings == 8
    assert old["revision"] == 4
    request = request.model_copy(update={"repin_dials": True})
    fresh, again = prepare_review(settings, store, _Saver(), request)
    assert fresh.llm_max_findings == 2
    assert again["revision"] == 1

    class _Graph:
        def __init__(self) -> None:
            self.state: object = None

        def invoke(self, state: object, config: object = None) -> object:
            del config
            self.state = state
            raise RuntimeError("basta")

    graph = _Graph()
    with pytest.raises(RuntimeError, match="basta"):
        process_request(graph, request, dial_snapshot=again)  # type: ignore[arg-type]
    assert isinstance(graph.state, dict)
    assert graph.state["dial_snapshot"] == again


def test_elige_la_foto_del_hilo_salvo_reintento_o_review_nueva() -> None:
    published = {"llm_max_findings": 4}
    checkpoint = {"dial_snapshot": {"revision": 2, "overrides": {"llm_max_findings": 9}}}
    assert choose_document(published=published, checkpoint=None, repin=False) == published
    assert choose_document(published=published, checkpoint=checkpoint, repin=False) == {
        "llm_max_findings": 9
    }
    assert choose_document(published=published, checkpoint=checkpoint, repin=True) == published
    assert choose_document(published=published, checkpoint={}, repin=False) == {}


def test_dos_guardados_con_la_misma_revision_solo_entra_uno() -> None:
    store = MemoryConfigStore()
    first = store.publish(expected=0, overrides={"llm_max_findings": 2}, actor="ana")
    assert first.revision == 1
    with pytest.raises(ConfigError):
        store.publish(expected=0, overrides={"llm_max_findings": 3}, actor="luis")
    assert store.read().overrides == {"llm_max_findings": 2}
    assert store.history()[0].actor == "ana"


def test_si_la_lectura_falla_se_queda_la_ultima_revision() -> None:
    class Flaky:
        def __init__(self) -> None:
            self.inner = MemoryConfigStore()
            self.fail = False

        def read(self):  # type: ignore[no-untyped-def]
            if self.fail:
                raise ConfigReadError("caido")
            return self.inner.read()

        def publish(self, **kwargs: object):  # type: ignore[no-untyped-def]
            return self.inner.publish(**kwargs)

        def history(self):  # type: ignore[no-untyped-def]
            return self.inner.history()

    flaky = Flaky()
    store = RememberingStore(flaky)  # type: ignore[arg-type]
    with pytest.raises(ConfigReadError):
        flaky.fail = True
        store.read()
    flaky.fail = False
    store.publish(expected=0, overrides={"scope_ignore": "vendor/"}, actor="ana")
    flaky.fail = True
    assert store.read().overrides == {"scope_ignore": "vendor/"}


def test_el_compare_and_swap_en_memoria_es_exclusivo() -> None:
    store = MemoryConfigStore()
    errors: list[BaseException] = []

    def publish() -> None:
        try:
            store.publish(expected=0, overrides={"llm_max_findings": 1}, actor="ana")
        except BaseException as exc:  # noqa: BLE001 - el test quiere el perdedor
            errors.append(exc)

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.read().revision == 1
    assert len(errors) == 1
