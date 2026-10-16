from __future__ import annotations

import pytest

from agent_review.adapters.budget_memory import InMemoryBudget
from agent_review.adapters.llm_fixer import FixBudgetExceeded, FixNotProposed, LlmFixer
from agent_review.domain.models import (
    AutofixClass,
    CommandResult,
    Finding,
    FixResult,
    FixStatus,
    Owner,
    Severity,
)
from agent_review.errors import TransientError
from agent_review.ports.llm import LlmReply


class _Llm:
    def __init__(
        self,
        reply: str,
        *,
        raises: Exception | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        self.reply = reply
        self._raises = raises
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self.calls: list[str] = []

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        json_mode=None,
        response_schema=None,
    ) -> LlmReply:
        del system, max_tokens, json_mode, response_schema
        self.calls.append(prompt)
        if self._raises is not None:
            raise self._raises
        return LlmReply(
            text=self.reply,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
        )


_FINDING = Finding(
    id="f1",
    title="Import sin usar",
    severity=Severity.P2,
    file="src/Foo.php",
    line=2,
    why_it_matters="Ruido",
    autofix_class=AutofixClass.GATED_AUTO,
    owner=Owner.HUMAN,
    requires_verification=True,
    confidence=75,
    evidence=["src/Foo.php:2 +use App\\Bar;"],
    suggested_fix="Quita el import",
)
_SOURCE = "<?php\nuse App\\Baz;\nfunction foo() {\n"


def test_propone_un_parche_desde_el_json_del_modelo() -> None:
    llm = _Llm('{"start_line": 2, "end_line": 2, "replacement": "use App\\\\Bar;"}')
    fixer = LlmFixer(llm=llm)
    patch = fixer.propose(_FINDING, source=_SOURCE, previous=None)
    assert patch.file == "src/Foo.php"
    assert patch.start_line == patch.end_line == 2
    assert "-use App\\Baz;" in patch.diff
    assert "+use App\\Bar;" in patch.diff
    # La pista de la persona viaja en el prompt.
    assert "Quita el import" in llm.calls[0]


def test_el_segundo_intento_recibe_el_stderr_del_primero() -> None:
    llm = _Llm('{"start_line": 2, "end_line": 2, "replacement": "x;"}')
    fixer = LlmFixer(llm=llm)
    previous = FixResult(
        finding_id="f1",
        status=FixStatus.FAILED,
        attempts=1,
        checks=[
            CommandResult(
                recipe="typecheck",
                argv=["make", "stan"],
                exit_code=1,
                duration_s=1.0,
                stderr_tail="Fatal error: no such class",
            )
        ],
        reason="typecheck fallo (exit 1)",
    )
    fixer.propose(_FINDING, source=_SOURCE, previous=previous)
    prompt = llm.calls[0]
    assert "INTENTO ANTERIOR FALLIDO" in prompt
    assert "no such class" in prompt


def test_respuesta_sin_rango_valido_no_produce_parche() -> None:
    fixer = LlmFixer(llm=_Llm('{"start_line": 0}'))
    with pytest.raises(FixNotProposed):
        fixer.propose(_FINDING, source=_SOURCE, previous=None)


def test_rango_mas_grande_que_el_tope_no_produce_parche() -> None:
    fixer = LlmFixer(llm=_Llm('{"start_line": 1, "end_line": 40, "replacement": "x"}'))
    with pytest.raises(FixNotProposed):
        fixer.propose(_FINDING, source=_SOURCE, previous=None)


def test_salida_no_json_no_produce_parche() -> None:
    fixer = LlmFixer(llm=_Llm("no soy json"))
    with pytest.raises(FixNotProposed):
        fixer.propose(_FINDING, source=_SOURCE, previous=None)


def test_presupuesto_agotado_no_llama_al_modelo() -> None:
    llm = _Llm('{"start_line": 2, "end_line": 2, "replacement": "x;"}')
    fixer = LlmFixer(llm=llm, budget=InMemoryBudget(total=1))
    with pytest.raises(FixBudgetExceeded):
        fixer.propose(_FINDING, source=_SOURCE, previous=None, review_key="r:1")
    assert llm.calls == []


def test_el_fixer_usa_su_propio_contador() -> None:
    # B11: el fix gasta contra **su** clave, distinta de la de la review. El
    # adaptador del worker la construye con el prefijo `budget:fix` (build_fixer);
    # aqui se comprueba que quien cobra es el contador del fix, no el de la review.
    llm = _Llm(
        '{"start_line": 2, "end_line": 2, "replacement": "use App\\\\Bar;"}',
        prompt_tokens=100,
        completion_tokens=20,
    )
    fix = InMemoryBudget(total=500_000)
    review = InMemoryBudget(total=500_000)
    fixer = LlmFixer(llm=llm, budget=fix)
    fixer.propose(_FINDING, source=_SOURCE, previous=None, review_key="r:1")
    assert fix.spent("r:1") == 120, "el fix cobra su gasto real en su contador"
    assert review.remaining("r:1") == 500_000, "el contador de la review no se toca"


def test_un_fallo_transitorio_devuelve_la_reserva_del_fix() -> None:
    # B8: sin la devolucion, el reintento del nodo reservaria dos veces.
    llm = _Llm("", raises=TransientError("boom"))
    budget = InMemoryBudget(total=500_000)
    fixer = LlmFixer(llm=llm, budget=budget)
    with pytest.raises(TransientError):
        fixer.propose(_FINDING, source=_SOURCE, previous=None, review_key="r:1")
    assert budget.reserved("r:1") == 0
