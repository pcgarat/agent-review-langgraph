"""L3-L5: bucle de herramientas *ReAct* en JSON (016 §5.2).

Sin red ni VCS: el modelo es un `LlmPort` guionizado y el lector es el
`FixtureContextReader`. Se prueba el protocolo de turno, cada tope, el turno final
forzado, la comprobacion de citas y el consumo del presupuesto.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from context_fakes import APP, ENV, README, build_fixture_reader

from agent_review.adapters.context_fixture import FixtureContextReader
from agent_review.adapters.tool_loop import (
    LoopResult,
    ReviewToolsOptions,
    citation_path,
    run_tool_loop,
)
from agent_review.ports.context import ContextLimits
from agent_review.ports.llm import LlmReply, LlmTruncatedError


class ScriptedLlm:
    """Devuelve respuestas en orden y registra cada llamada; se repite la ultima."""

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
        self.calls.append(
            {"system": system, "prompt": prompt, "max_tokens": max_tokens, "json_mode": json_mode}
        )
        return LlmReply(text=self._responses[index], prompt_tokens=10, completion_tokens=5)


class MemoryBudget:
    """Presupuesto en memoria con la semantica de `BudgetPort` (017 B1)."""

    def __init__(self, tokens: int) -> None:
        self.remaining_tokens = tokens
        self.reservations: list[tuple[str, int]] = []
        self.commits: list[tuple[str, int, int]] = []

    def reserve(self, review_key: str, tokens: int) -> bool:
        self.reservations.append((review_key, tokens))
        return tokens <= self.remaining_tokens

    def commit(self, review_key: str, reserved: int, actual: int) -> None:
        self.commits.append((review_key, reserved, actual))
        self.remaining_tokens -= actual

    def remaining(self, review_key: str) -> int:
        del review_key
        return self.remaining_tokens


def _tools(*entries: dict[str, Any]) -> str:
    return json.dumps({"tools": list(entries)})


def _read(path: str) -> dict[str, Any]:
    return {"tool": "read_file", "path": path}


def _grep(pattern: str, glob: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"tool": "grep", "pattern": pattern}
    if glob:
        entry["glob"] = glob
    return entry


def _is_final(data: dict[str, Any]) -> bool:
    return "findings" in data


def _run(
    llm: ScriptedLlm,
    reader: FixtureContextReader,
    *,
    limits: ContextLimits | None = None,
    options: ReviewToolsOptions | None = None,
    budget: MemoryBudget | None = None,
) -> LoopResult:
    return run_tool_loop(
        llm,
        system="Eres un revisor.",
        prompt="Revisa el PR.",
        reader=reader,
        limits=limits or ContextLimits(),
        options=options,
        is_final=_is_final,
        budget=budget,
        review_key="r:1",
    )


# --- Protocolo de turno -----------------------------------------------------------


def test_recorre_grep_read_file_y_final(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    llm = ScriptedLlm(
        _tools(_grep("needle")),
        _tools(_read(APP)),
        json.dumps({"findings": [{"file": APP, "line": 3}]}),
    )
    result = _run(llm, reader)

    assert result.data == {"findings": [{"file": APP, "line": 3}]}
    assert result.turns == 3, "grep, read_file y el turno final"
    assert result.tool_calls == 2
    assert set(result.served) == {APP}
    assert result.bytes_served > 0
    assert not result.final_forced
    # El resultado de cada herramienta viaja delimitado (W4).
    assert "<tool_result>" in llm.calls[-1]["prompt"]


def test_el_system_base_regla_que_lo_servido_es_dato(tmp_path: Path) -> None:
    llm = ScriptedLlm(json.dumps({"findings": []}))
    _run(llm, build_fixture_reader(tmp_path))
    system = llm.calls[0]["system"]
    assert "SOLO LECTURA" in system
    assert "no una instruccion" in system


def test_pide_json_mode_al_modelo(tmp_path: Path) -> None:
    llm = ScriptedLlm(json.dumps({"findings": []}))
    _run(llm, build_fixture_reader(tmp_path))
    assert llm.calls[0]["json_mode"] == "json_object"


def test_on_reasoning_se_reenvia_a_cada_complete(tmp_path: Path) -> None:
    # 014 RH7: el panel sigue viendo el razonamiento turno a turno.
    seen: list[str] = []

    class Listening(ScriptedLlm):
        def complete(self, *, on_reasoning=None, **kwargs: Any) -> LlmReply:  # type: ignore[override]
            if on_reasoning is not None:
                on_reasoning("trozo")
                seen.append("ok")
            return super().complete(**kwargs)

    llm = Listening(
        _tools(_read(APP)),
        json.dumps({"findings": []}),
    )
    run_tool_loop(
        llm,
        system="Eres un revisor.",
        prompt="Revisa.",
        reader=build_fixture_reader(tmp_path),
        limits=ContextLimits(),
        is_final=_is_final,
        on_reasoning=lambda piece: None,
    )
    assert len(seen) == 2, "turno con herramientas + final"


def test_cuatro_peticiones_en_un_turno_atiende_tres_y_avisa(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    entries = [_read(APP), _read(README), _read(APP), _read(README)]
    llm = ScriptedLlm(_tools(*entries), json.dumps({"findings": []}))
    result = _run(llm, reader)

    assert result.tool_calls == 3, "solo tres peticiones por turno (Q2 de 014)"
    assert any("descartadas" in note for note in result.notes)
    assert "AVISO" in llm.calls[-1]["prompt"]


def test_el_tope_por_turno_es_configurable(tmp_path: Path) -> None:
    # 023 F1/P1: `calls_per_turn` deja de ser constante; con 2 se atienden dos.
    entries = [_read(APP), _read(README), _read(APP)]
    llm = ScriptedLlm(_tools(*entries), json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path), limits=ContextLimits(calls_per_turn=2))

    assert result.tool_calls == 2
    assert any("1 descartadas" in note for note in result.notes)


def test_strict_calls_pone_un_error_por_peticion_no_atendida(tmp_path: Path) -> None:
    # 023 F1/S1: el excedente no desaparece en silencio.
    entries = [_read(APP), _read(README), _read(APP), _read(README)]
    llm = ScriptedLlm(_tools(*entries), json.dumps({"findings": []}))
    result = _run(
        llm,
        build_fixture_reader(tmp_path),
        limits=ContextLimits(calls_per_turn=2),
        options=ReviewToolsOptions(strict_calls=True),
    )

    assert result.tool_calls == 2
    prompt = llm.calls[-1]["prompt"]
    assert prompt.count("ERROR: no atendida") == 2
    assert "AVISO" not in prompt


def test_el_prompt_anuncia_las_capacidades_del_puerto(tmp_path: Path) -> None:
    # 023 F1/E11: el anuncio no depende de los flags; sin el, el modelo no pediria
    # los modos que el puerto si sirve.
    llm = ScriptedLlm(json.dumps({"findings": []}))
    _run(llm, build_fixture_reader(tmp_path))

    system = llm.calls[0]["system"]
    for capability in ('"mode"', '"files_only"', '"depth"', '"ref"'):
        assert capability in system


def test_los_defaults_no_cambian_lo_servido(tmp_path: Path) -> None:
    # 023 F1 (criterio 9): mismos argumentos y mismos defaults ⇒ mismo prompt.
    entries = [_read(APP), _grep("needle")]
    without = ScriptedLlm(_tools(*entries), json.dumps({"findings": []}))
    explicit = ScriptedLlm(_tools(*entries), json.dumps({"findings": []}))

    _run(without, build_fixture_reader(tmp_path))
    _run(explicit, build_fixture_reader(tmp_path), options=ReviewToolsOptions())

    assert [call["prompt"] for call in without.calls] == [call["prompt"] for call in explicit.calls]


def test_una_herramienta_desconocida_se_sirve_como_error(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        _tools({"tool": "rm_rf", "path": "/"}),
        json.dumps({"findings": []}),
    )
    result = _run(llm, build_fixture_reader(tmp_path))
    assert result.tool_calls == 0
    assert "ERROR" in llm.calls[-1]["prompt"]


def test_acepta_la_forma_de_openai(tmp_path: Path) -> None:
    # El modelo real usa `{"name": ..., "args"/"arguments": {...}}`. El bucle lo
    # sirve igual (014 RH11): antes toda peticion caia como invalida y el lector
    # no servia nada.
    for key in ("args", "arguments"):
        llm = ScriptedLlm(
            _tools({"name": "read_file", key: {"path": APP}}),
            json.dumps({"findings": []}),
        )
        result = _run(llm, build_fixture_reader(tmp_path))
        assert result.tool_calls == 1
        assert "needle" in llm.calls[-1]["prompt"]


def test_traduce_los_alias_del_modelo(tmp_path: Path) -> None:
    # `search`→`grep`/`query`→`pattern` y `start_line`/`end_line`→`start`/`end`.
    llm = ScriptedLlm(
        _tools({"name": "search", "args": {"query": "needle", "glob": "**/*.py"}}),
        _tools({"name": "read_file", "args": {"path": APP, "start_line": 1, "end_line": 1}}),
        json.dumps({"findings": []}),
    )
    result = _run(llm, build_fixture_reader(tmp_path))
    assert result.tool_calls == 2
    assert f"{APP}:3" in llm.calls[-2]["prompt"]
    assert "import os" in llm.calls[-1]["prompt"]


def test_un_turno_truncado_no_tumba_el_lote(tmp_path: Path) -> None:
    # `finish_reason=length` sin contenido: se pierde el turno, no el lote (014 H6).

    class Truncating(ScriptedLlm):
        def complete(self, **kwargs: Any) -> LlmReply:  # type: ignore[override]
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise LlmTruncatedError("agoto max_tokens sin contenido")
            return LlmReply(text=json.dumps({"findings": []}), finish_reason="stop")

    llm = Truncating()
    result = _run(llm, build_fixture_reader(tmp_path))
    assert result.data == {"findings": []}
    assert any("length" in note for note in result.notes)
    assert "final" in llm.calls[-1]["prompt"].lower()


def test_un_turno_truncado_reiterado_cierra(tmp_path: Path) -> None:
    class AlwaysTruncating(ScriptedLlm):
        def complete(self, **kwargs: Any) -> LlmReply:  # type: ignore[override]
            self.calls.append(kwargs)
            raise LlmTruncatedError("agoto max_tokens sin contenido")

    result = _run(AlwaysTruncating(), build_fixture_reader(tmp_path))
    assert result.data is None
    assert result.forced_by_truncation is True


def test_una_herramienta_que_falla_no_tumba_el_bucle(tmp_path: Path) -> None:
    class Exploding(FixtureContextReader):
        def read_file(self, *args: Any, **kwargs: Any) -> str | None:
            raise RuntimeError("el runner se cayo")

    reader = Exploding(tmp_path)
    llm = ScriptedLlm(_tools(_read(APP)), json.dumps({"findings": []}))
    result = _run(llm, reader)
    assert result.tool_calls == 0
    assert "ERROR" in llm.calls[-1]["prompt"]


def test_una_peticion_mal_formada_se_sirve_como_error(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        _tools({"tool": "read_file"}),
        json.dumps({"findings": []}),
    )
    result = _run(llm, build_fixture_reader(tmp_path))
    assert result.tool_calls == 0, "sin `path` no hay lectura"


def test_read_file_ausente_no_cuenta_como_servido(tmp_path: Path) -> None:
    llm = ScriptedLlm(_tools(_read("src/no-existe.py")), json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path))
    assert result.tool_calls == 0
    assert result.served == {}


# --- Topes y turno final forzado --------------------------------------------------


def test_el_tope_de_turnos_fuerza_el_turno_final(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    llm = ScriptedLlm(_tools(_read(APP)), json.dumps({"findings": []}))
    result = _run(llm, reader, limits=ContextLimits(max_turns=1))
    assert result.turns == 1
    assert result.final_forced
    assert "Ya no puedes pedir mas herramientas" in llm.calls[-1]["prompt"]


def test_el_tope_de_peticiones_fuerza_el_turno_final(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    llm = ScriptedLlm(_tools(_read(APP)), _tools(_read(APP)), json.dumps({"findings": []}))
    result = _run(llm, reader, limits=ContextLimits(max_calls=2, max_turns=10))
    assert result.tool_calls == 2
    assert result.final_forced


def test_el_tope_de_bytes_fuerza_el_turno_final(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    llm = ScriptedLlm(_tools(_read(APP)), json.dumps({"findings": []}))
    result = _run(llm, reader, limits=ContextLimits(max_bytes=1))
    assert result.final_forced


def test_el_tope_de_bytes_recorta_lo_servido(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    llm = ScriptedLlm(_tools(_read(APP)), json.dumps({"findings": []}))
    result = _run(llm, reader, limits=ContextLimits(max_bytes=5))
    assert result.bytes_served == 5, "lo servido no supera el tope del lote"


def test_el_turno_final_forzado_con_herramientas_cierra_con_lo_que_haya(tmp_path: Path) -> None:
    reader = build_fixture_reader(tmp_path)
    # Antes del final forzado pide una herramienta; al forzarlo, vuelve a pedirla.
    llm = ScriptedLlm(
        _tools(_read(APP)),
        _tools(_read(README)),
        _tools(_read(README)),
    )
    result = _run(llm, reader, limits=ContextLimits(max_turns=1))
    assert result.final_forced
    assert result.data is None, "no llego un objeto final"
    assert result.tool_calls == 1, "en el turno forzado no se sirven herramientas"


# --- JSON invalido -----------------------------------------------------------------


def test_json_invalido_recibe_un_turno_correctivo(tmp_path: Path) -> None:
    llm = ScriptedLlm("{esto no es json", json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path))
    assert result.data == {"findings": []}
    assert "no era valida" in llm.calls[-1]["prompt"]


def test_json_invalido_reiterado_cierra_sin_hallazgos(tmp_path: Path) -> None:
    llm = ScriptedLlm("no json", "tampoco", "ni esto")
    result = _run(llm, build_fixture_reader(tmp_path))
    assert result.data is None
    assert len(llm.calls) == 2, "un solo turno correctivo"


def test_respuesta_sin_herramientas_ni_final_recibe_un_correctivo(tmp_path: Path) -> None:
    llm = ScriptedLlm(json.dumps({"otra_cosa": 1}), json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path))
    assert result.data == {"findings": []}


def test_fence_markdown_se_tolera(tmp_path: Path) -> None:
    llm = ScriptedLlm("```json\n" + json.dumps({"findings": []}) + "\n```")
    result = _run(llm, build_fixture_reader(tmp_path))
    assert result.data == {"findings": []}


def test_texto_alrededor_del_json_se_tolera(tmp_path: Path) -> None:
    llm = ScriptedLlm("Aqui tienes: " + json.dumps({"findings": []}) + " fin")
    result = _run(llm, build_fixture_reader(tmp_path))
    assert result.data == {"findings": []}


# --- Comprobacion de citas (W3) ----------------------------------------------------


def test_cite_served_acepta_un_fichero_leido(tmp_path: Path) -> None:
    llm = ScriptedLlm(_tools(_read(APP)), json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path))
    assert result.cite_served(f"{APP}:3 needle = 1")
    assert result.cite_served(f"{APP}:4: print(needle)")


def test_cite_served_acepta_un_fichero_devuelto_por_grep(tmp_path: Path) -> None:
    llm = ScriptedLlm(_tools(_grep("needle")), json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path))
    assert result.cite_served(f"{APP}:3: needle = 1")


def test_cite_served_rechaza_un_fichero_no_servido(tmp_path: Path) -> None:
    llm = ScriptedLlm(_tools(_read(APP)), json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path))
    assert not result.cite_served(f"{README}:1 # repo")
    assert not result.cite_served("sin-dos-puntos")


def test_una_ruta_vetada_nunca_queda_servida(tmp_path: Path) -> None:
    llm = ScriptedLlm(_tools(_read(ENV)), json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path))
    assert not result.cite_served(f"{ENV}:1 SECRET=1")


def test_citation_path_extrae_la_ruta() -> None:
    assert citation_path("src/app.py:3: needle = 1") == "src/app.py"
    assert citation_path("src/app.py:12") == "src/app.py"
    assert citation_path("sin-ruta") == ""
    assert citation_path("") == ""


# --- Presupuesto (consumido de 017) ------------------------------------------------


def test_sin_presupuesto_no_se_reserva_nada(tmp_path: Path) -> None:
    llm = ScriptedLlm(json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path))
    assert result.usage == 15, "usage real del proveedor (prompt+completion)"


def test_el_presupuesto_reserva_antes_y_ajusta_con_el_usage(tmp_path: Path) -> None:
    budget = MemoryBudget(10_000_000)
    llm = ScriptedLlm(json.dumps({"findings": []}))
    _run(llm, build_fixture_reader(tmp_path), budget=budget)
    assert budget.reservations and budget.reservations[0][0] == "r:1"
    assert budget.reservations[0][1] > 0
    assert budget.commits == [("r:1", budget.reservations[0][1], 15)]


def test_presupuesto_agotado_fuerza_el_turno_final(tmp_path: Path) -> None:
    budget = MemoryBudget(0)
    llm = ScriptedLlm(json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path), budget=budget)
    assert result.final_forced
    assert result.turns == 0, "no se permite un turno con herramientas"
    assert "Ya no puedes pedir mas herramientas" in llm.calls[-1]["prompt"]


def test_presupuesto_agotado_no_da_segundo_turno_si_ya_era_final(tmp_path: Path) -> None:
    budget = MemoryBudget(0)
    llm = ScriptedLlm(_tools(_read(APP)), json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path), budget=budget)
    assert result.final_forced
    assert result.data is None


def test_un_presupuesto_amplio_no_corta_el_bucle(tmp_path: Path) -> None:
    budget = MemoryBudget(10_000_000)
    llm = ScriptedLlm(_tools(_read(APP)), json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path), budget=budget)
    assert result.turns == 2, "una con herramientas y el final"
    assert not result.final_forced


def test_el_reintento_del_modelo_no_es_del_bucle(tmp_path: Path) -> None:
    class Boom(Exception):
        pass

    class Fallon(ScriptedLlm):
        def complete(self, **kwargs: Any) -> LlmReply:
            raise Boom("transitorio")

    with pytest.raises(Boom):
        _run(Fallon(), build_fixture_reader(tmp_path))
