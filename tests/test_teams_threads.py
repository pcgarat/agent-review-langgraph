"""T4 (iniciativa 013): hilo por PR — mapeo, mensaje raiz, reply y edicion.

El transporte se prueba con un `ChannelClient` falso: la logica de hilo (C6/C7)
es la que importa, no el SDK. El mapeo (`ChannelStore`) tiene su propia seccion,
incluida la tolerancia a entradas corruptas (perder el hilo solo abre uno nuevo).
"""

from __future__ import annotations

import logging

import pytest

from agent_review.teams_bot.threads import (
    CardRef,
    ChannelContext,
    InMemoryChannelStore,
    RedisChannelStore,
    ThreadRef,
    card_field,
    thread_field,
)
from agent_review.teams_bot.transport import ChannelClient, ChannelTransport


class FakeChannel:
    """Cliente del canal en memoria: registra cada publicacion y devuelve ids."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, object, str]] = []
        self.replies: list[tuple[str, str, object, str]] = []
        self.updates: list[tuple[str, str, object, str]] = []
        self.deletes: list[tuple[str, str, str]] = []
        self._next = 100

    async def send(self, conversation_id: str, payload: object, *, service_url: str = "") -> str:
        self.sends.append((conversation_id, payload, service_url))
        self._next += 1
        return str(self._next)

    async def reply(
        self,
        conversation_id: str,
        root_message_id: str,
        payload: object,
        *,
        service_url: str = "",
    ) -> str:
        self.replies.append((conversation_id, root_message_id, payload, service_url))
        self._next += 1
        return str(self._next)

    async def update(
        self, conversation_id: str, message_id: str, payload: object, *, service_url: str = ""
    ) -> None:
        self.updates.append((conversation_id, message_id, payload, service_url))

    async def delete(self, conversation_id: str, message_id: str, *, service_url: str = "") -> None:
        self.deletes.append((conversation_id, message_id, service_url))


def _transport(client: FakeChannel, store: InMemoryChannelStore) -> ChannelTransport:
    return ChannelTransport(client=client, store=store)


def _with_channel() -> InMemoryChannelStore:
    store = InMemoryChannelStore()
    store.remember_context(
        ChannelContext(conversation_id="19:canal@thread.tacv2", service_url="https://smba")
    )
    return store


# --- Hilo por PR (C6) ---------------------------------------------------------


async def test_el_primer_aviso_abre_el_hilo_raiz_del_pr() -> None:
    client, store = FakeChannel(), _with_channel()

    posted = await _transport(client, store).post_text(repo="r", pr_id=7, text="Review publicada")

    assert posted is True
    assert len(client.sends) == 1, "el primer aviso es raiz, no reply"
    assert client.replies == []
    ref = store.thread(repo="r", pr_id=7)
    assert ref is not None
    assert ref.conversation_id == "19:canal@thread.tacv2"
    assert ref.root_message_id == "101"
    assert ref.service_url == "https://smba", "el service_url se recuerda para el reply"


async def test_los_avisos_siguientes_del_mismo_pr_van_al_hilo() -> None:
    client, store = FakeChannel(), _with_channel()
    transport = _transport(client, store)
    await transport.post_text(repo="r", pr_id=7, text="uno")

    await transport.post_text(repo="r", pr_id=7, text="dos")

    assert len(client.sends) == 1, "no se abre un hilo nuevo"
    assert len(client.replies) == 1
    conversation_id, root_id, payload, service_url = client.replies[0]
    assert (conversation_id, root_id, payload, service_url) == (
        "19:canal@thread.tacv2",
        "101",
        "dos",
        "https://smba",
    )


async def test_dos_prs_distintos_abren_dos_hilos() -> None:
    client, store = FakeChannel(), _with_channel()
    transport = _transport(client, store)

    await transport.post_text(repo="r", pr_id=7, text="uno")
    await transport.post_text(repo="r", pr_id=8, text="dos")

    assert len(client.sends) == 2, "cada PR tiene su propio mensaje raiz"
    assert store.thread(repo="r", pr_id=7) != store.thread(repo="r", pr_id=8)


async def test_sin_canal_capturado_no_se_publica() -> None:
    """C14: sin `conversation_id` no hay destino proactivo todavia."""
    client, store = FakeChannel(), InMemoryChannelStore()

    posted = await _transport(client, store).post_text(repo="r", pr_id=7, text="uno")

    assert posted is False
    assert client.sends == [] and client.replies == []


async def test_perder_el_hilo_crea_uno_nuevo_sin_romper() -> None:
    """C6: el hilo es UI, no verdad. Borrar el mapeo solo abre otro hilo."""
    client, store = FakeChannel(), _with_channel()
    transport = _transport(client, store)
    await transport.post_text(repo="r", pr_id=7, text="uno")

    # El mapeo se pierde (Redis reiniciado).
    store._threads.clear()
    await transport.post_text(repo="r", pr_id=7, text="dos")

    assert len(client.sends) == 2, "se abre un hilo nuevo"
    assert client.replies == []


# --- Tarjeta editable (C7) ----------------------------------------------------


async def test_la_tarjeta_se_publica_en_el_hilo_y_se_recuerda() -> None:
    client, store = FakeChannel(), _with_channel()
    transport = _transport(client, store)
    await transport.post_text(repo="r", pr_id=7, text="review")
    card = {"type": "AdaptiveCard"}

    posted = await transport.post_card("r:7:abc", repo="r", pr_id=7, card=card)

    assert posted is True
    assert client.replies[-1][2] == card, "va como reply del hilo, no como raiz"
    ref = store.card("r:7:abc")
    assert ref is not None and ref.message_id == "102"


async def test_la_misma_tarjeta_se_edita_en_vez_de_duplicarse() -> None:
    client, store = FakeChannel(), _with_channel()
    transport = _transport(client, store)
    await transport.post_text(repo="r", pr_id=7, text="review")
    await transport.post_card("r:7:abc", repo="r", pr_id=7, card={"v": 1})

    await transport.post_card("r:7:abc", repo="r", pr_id=7, card={"v": 2})

    assert len(client.updates) == 1, "la segunda vez edita, no publica otra tarjeta"
    conversation_id, message_id, payload, service_url = client.updates[0]
    assert (conversation_id, message_id, service_url) == (
        "19:canal@thread.tacv2",
        "102",
        "https://smba",
    )
    assert payload == {"v": 2}


async def test_una_tarjeta_del_primer_pr_abre_el_hilo_como_raiz() -> None:
    """La primera review de un PR ya pausada: la tarjeta es el mensaje raiz."""
    client, store = FakeChannel(), _with_channel()

    posted = await _transport(client, store).post_card("r:7:abc", repo="r", pr_id=7, card={"v": 1})

    assert posted is True
    assert len(client.sends) == 1 and client.replies == []
    assert store.thread(repo="r", pr_id=7) is not None, "el hilo queda abierto"
    card = store.card("r:7:abc")
    assert card is not None and card.message_id == store.thread(repo="r", pr_id=7).root_message_id


# --- Mapeo tolerante a entradas corruptas -------------------------------------


def test_un_root_message_id_no_numerico_se_trata_como_hilo_no_iniciado() -> None:
    """El SDK exige un id numerico (T0): mejor un hilo nuevo que un error."""
    store = InMemoryChannelStore()
    store._threads[thread_field("r", 7)] = (
        '{"conversation_id": "c", "root_message_id": "root-1", "service_url": "s"}'
    )

    assert store.thread(repo="r", pr_id=7) is None


def test_una_entrada_ilegible_se_trata_como_hilo_no_iniciado() -> None:
    store = InMemoryChannelStore()
    store._threads[thread_field("r", 7)] = "no es json"

    assert store.thread(repo="r", pr_id=7) is None


def test_el_campo_del_hash_es_estable_y_corto() -> None:
    """El `repo:pr` se hashea: no cabe como clave y sus `:` serian ambiguos."""
    assert thread_field("cqm/emotivoo-v2", 7) == thread_field("cqm/emotivoo-v2", 7)
    assert thread_field("cqm/emotivoo-v2", 7) != thread_field("cqm/emotivoo-v2", 8)
    assert len(thread_field("cqm/emotivoo-v2", 7)) == 12
    assert ":" not in thread_field("cqm/emotivoo-v2", 7)
    assert len(card_field("cqm/emotivoo-v2:7:abc")) == 12


def test_el_store_en_memoria_guarda_canal_hilo_y_tarjeta() -> None:
    store = InMemoryChannelStore()
    store.remember_context(ChannelContext(conversation_id="c", service_url="s"))
    store.remember_thread(repo="r", pr_id=1, ref=ThreadRef("c", "9", "s"))
    store.remember_card("k", CardRef("c", "9", "s"))

    assert store.context() == ChannelContext(conversation_id="c", service_url="s")
    assert store.thread(repo="r", pr_id=1) == ThreadRef("c", "9", "s")
    assert store.card("k") == CardRef("c", "9", "s")
    assert store.open_card_keys() == ["k"]
    store.forget_card("k")
    assert store.card("k") is None
    assert store.open_card_keys() == []


# El protocolo del cliente falso cubre lo que exige el transporte.
def test_el_cliente_falso_cumple_el_puerto() -> None:
    assert isinstance(FakeChannel(), ChannelClient)


def test_un_pr_nuevo_en_redis_no_se_registra_como_entrada_ilegible(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un `HGET` sin entrada es lo normal en cada PR nuevo, no una corrupcion."""

    class _EmptyRedis:
        def hget(self, key: str, field: str) -> None:
            return None

    store = RedisChannelStore(url="redis://localhost:6399/0")
    store._client = _EmptyRedis()  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger="agent_review.teams_bot.threads"):
        assert store.thread(repo="r", pr_id=1) is None
        assert store.card("k") is None

    assert "ilegible" not in caplog.text
