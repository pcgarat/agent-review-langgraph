"""P1-P4: presupuesto por review (017).

El adaptador en memoria fija la semantica del puerto (B9); el de Redis la reproduce
con un script atomico y se prueba contra `fakeredis`. El bucle de 016 la consume:
`reserve(False)` corta (B7) y un fallo del modelo devuelve la reserva (B8).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import fakeredis
import pytest
from context_fakes import APP, build_fixture_reader

from agent_review.adapters.budget_memory import InMemoryBudget
from agent_review.adapters.budget_redis import RedisBudget
from agent_review.adapters.tool_loop import LoopResult, run_tool_loop
from agent_review.ports.budget import BudgetPort, release_tokens, reserve_tokens
from agent_review.ports.context import ContextLimits
from agent_review.ports.llm import LlmReply


def test_es_un_budget_port() -> None:
    assert isinstance(InMemoryBudget(100), BudgetPort)
    assert isinstance(_redis_budget(100), BudgetPort)


def _redis_budget(total: int, *, ttl_s: int = 3600) -> RedisBudget:
    """Adaptador Redis sobre `fakeredis`: ejecuta el script Lua de verdad."""
    return RedisBudget(
        url="redis://localhost:6379/0",
        total=total,
        ttl_s=ttl_s,
        client=fakeredis.FakeRedis(decode_responses=True),
    )


# --- Semantica del puerto (B8, B9), sobre los dos adaptadores ---------------------


@pytest.fixture(params=["memoria", "redis"])
def budget_factory(request: pytest.FixtureRequest) -> Any:
    """Los dos adaptadores deben compartir la semantica observable (R6)."""
    if request.param == "redis":
        return lambda total, ttl_s=3600: _redis_budget(total, ttl_s=ttl_s)
    return lambda total, ttl_s=3600: InMemoryBudget(total)


def test_remaining_descuenta_lo_reservado_y_no_confirmado(budget_factory: Any) -> None:
    """B9: si `remaining` ignorara lo pendiente, dos ramas verian el mismo saldo."""
    budget = budget_factory(1000)
    assert budget.reserve("r:1", 400)
    assert budget.remaining("r:1") == 600, "resta la reserva aunque no se haya confirmado"


def test_reserve_no_pasa_si_no_cabe(budget_factory: Any) -> None:
    budget = budget_factory(100)
    assert budget.reserve("r:1", 80)
    assert not budget.reserve("r:1", 30), "80 + 30 > 100"
    assert budget.remaining("r:1") == 20, "una reserva rechazada no toca el contador"


def test_commit_devuelve_lo_reservado_que_no_se_gasto(budget_factory: Any) -> None:
    """B8: el camino del error confirma el gasto real (0) y libera el resto."""
    budget = budget_factory(1000)
    budget.reserve("r:1", 400)
    budget.commit("r:1", 400, 50)
    assert budget.remaining("r:1") == 950, "solo cuentan los 50 gastados"


def test_commit_con_actual_cero_libera_toda_la_reserva(budget_factory: Any) -> None:
    budget = budget_factory(1000)
    budget.reserve("r:1", 400)
    budget.commit("r:1", 400, 0)
    assert budget.remaining("r:1") == 1000, "la reserva se devuelve entera"


def test_un_commit_doble_no_rompe_el_contador(budget_factory: Any) -> None:
    """Un consumidor que confirma dos veces el mismo tramo no puede dar saldo extra."""
    budget = budget_factory(1000)
    budget.reserve("r:1", 400)
    budget.commit("r:1", 400, 0)
    budget.commit("r:1", 400, 0)
    assert budget.remaining("r:1") == 1000, "no se libera mas de lo reservado"


def test_no_se_puede_gastar_mas_que_el_total(budget_factory: Any) -> None:
    budget = budget_factory(100)
    assert budget.reserve("r:1", 60)
    assert budget.reserve("r:1", 40)
    assert budget.remaining("r:1") == 0
    budget.commit("r:1", 60, 60)
    budget.commit("r:1", 40, 40)
    assert budget.remaining("r:1") == 0
    assert not budget.reserve("r:1", 1)


def test_los_contadores_son_por_review(budget_factory: Any) -> None:
    """Dos reviews no se pisan el presupuesto."""
    budget = budget_factory(1000)
    assert budget.reserve("r:1", 900)
    assert budget.remaining("r:2") == 1000, "otra review empieza con el total"


# --- Helpers B7/B8 ----------------------------------------------------------------


def test_reserve_tokens_sin_puerto_deja_pasar() -> None:
    assert reserve_tokens(None, "r:1", 10_000)
    assert reserve_tokens(InMemoryBudget(100), "", 10_000), "sin clave no se consulta"


def test_release_tokens_sin_puerto_o_sin_reserva_no_hace_nada() -> None:
    budget = InMemoryBudget(total=1000)
    release_tokens(None, "r:1", 400)
    release_tokens(budget, "r:1", 0)
    assert budget.remaining("r:1") == 1000


# --- El bucle de 016 consume el puerto -------------------------------------------


class ScriptedLlm:
    def __init__(self, *responses: str) -> None:
        self._responses = list(responses) or ["{}"]
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        json_mode: str | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> LlmReply:
        del response_schema
        index = min(len(self.calls), len(self._responses) - 1)
        self.calls.append({"system": system, "prompt": prompt})
        return LlmReply(text=self._responses[index], prompt_tokens=10, completion_tokens=5)


class StubbornBudget(InMemoryBudget):
    """Reserva siempre rechazada con saldo alto: fuerza el caso de D21."""

    def reserve(self, review_key: str, tokens: int) -> bool:
        return False


def _tools(*entries: dict[str, Any]) -> str:
    return json.dumps({"tools": list(entries)})


def _run(
    llm: ScriptedLlm,
    reader: Any,
    *,
    budget: BudgetPort | None = None,
) -> LoopResult:
    return run_tool_loop(
        llm,
        system="Eres un revisor.",
        prompt="Revisa el PR.",
        reader=reader,
        limits=ContextLimits(),
        is_final=lambda data: "findings" in data,
        budget=budget,
        review_key="r:1",
    )


def test_reserve_false_corta_igual_que_un_remaining_corto(tmp_path: Path) -> None:
    """B7/D21: el modelo no gasta un turno con herramientas si `reserve` dice no."""
    budget = StubbornBudget(total=10_000_000)
    llm = ScriptedLlm(_tools({"tool": "read_file", "path": APP}), json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path), budget=budget)
    assert result.final_forced
    assert result.turns == 0, "ningun turno con herramientas llego a contar"
    assert "Ya no puedes pedir mas herramientas" in llm.calls[-1]["prompt"]


def test_un_fallo_del_modelo_devuelve_la_reserva(tmp_path: Path) -> None:
    """B8: el reintento del nodo no debe reservar dos veces."""
    budget = InMemoryBudget(total=10_000_000)

    class Fallon(ScriptedLlm):
        def complete(self, **kwargs: Any) -> LlmReply:
            raise RuntimeError("transitorio")

    with pytest.raises(RuntimeError):
        _run(Fallon(), build_fixture_reader(tmp_path), budget=budget)
    assert budget.remaining("r:1") == 10_000_000, "la reserva del intento fallido se devuelve"
    assert budget.spent("r:1") == 0


def test_el_salvamento_sin_reserva_no_libera_la_de_otra_rama(tmp_path: Path) -> None:
    """B12: el turno final sin reserva cobra lo gastado sin tocar lo de las demás.

    Otra rama de la misma review tiene una reserva pendiente que deja al bucle sin
    saldo. El salvamento llama igualmente (B4) y cobra su gasto real, pero la
    reserva ajena sigue intacta: liberarla dejaría pasar a una tercera rama.
    """
    budget = InMemoryBudget(total=20_000)
    assert budget.reserve("r:1", 17_000), "la otra rama reserva primero"
    llm = ScriptedLlm(json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path), budget=budget)
    assert result.final_forced
    assert len(llm.calls) == 1, "el turno de salvamento se hace"
    assert budget.reserved("r:1") == 17_000, "la reserva de la otra rama sigue entera"
    assert budget.spent("r:1") == 15, "el gasto real del salvamento se cobra"


def test_un_fallo_del_salvamento_no_libera_la_reserva_de_otra_rama(tmp_path: Path) -> None:
    """B8 + B12: el camino del error solo devuelve lo que esta llamada reservó."""
    budget = InMemoryBudget(total=20_000)
    assert budget.reserve("r:1", 17_000)

    class Fallon(ScriptedLlm):
        def complete(self, **kwargs: Any) -> LlmReply:
            raise RuntimeError("transitorio")

    with pytest.raises(RuntimeError):
        _run(Fallon(), build_fixture_reader(tmp_path), budget=budget)
    assert budget.reserved("r:1") == 17_000, "no se devuelve una reserva que no se hizo"
    assert budget.spent("r:1") == 0
