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
from agent_review.ports.context import (
    ContextLimits,
    ContextReaderPort,
    ReadingRegistry,
    ReadResult,
    ToolError,
    ToolResult,
)
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


def _read(path: str, **args: Any) -> dict[str, Any]:
    return {"tool": "read_file", "path": path, **args}


def _grep(pattern: str, glob: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"tool": "grep", "pattern": pattern}
    if glob:
        entry["glob"] = glob
    return entry


def _is_final(data: dict[str, Any]) -> bool:
    return "findings" in data


def _run(
    llm: ScriptedLlm,
    reader: ContextReaderPort,
    *,
    limits: ContextLimits | None = None,
    options: ReviewToolsOptions | None = None,
    budget: MemoryBudget | None = None,
    readings: ReadingRegistry | None = None,
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
        readings=readings,
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
        def read_file(self, *args: Any, **kwargs: Any) -> ReadResult | None:
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


def test_cite_served_no_acepta_un_fichero_solo_visto_por_grep(tmp_path: Path) -> None:
    # 023 S1: `grep` devuelve coincidencias, no el cuerpo; una cita no queda probada.
    llm = ScriptedLlm(_tools(_grep("needle")), json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path))
    assert not result.cite_served(f"{APP}:3: needle = 1")


def test_cite_served_acota_a_las_lineas_del_rango_servido(tmp_path: Path) -> None:
    # 023 S1: leer `1..2` prueba esas lineas y no las demas.
    llm = ScriptedLlm(_tools(_read(APP, start=1, end=2)), json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path))
    assert result.cite_served(f"{APP}:1 import os")
    assert not result.cite_served(f"{APP}:3: needle = 1")


def test_cite_served_no_mezcla_la_ref_base_con_head(tmp_path: Path) -> None:
    # 023 S1: leer `base` no prueba lo que el hallazgo afirma de `head`.
    llm = ScriptedLlm(_tools(_read(APP, ref="base")), json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path))
    assert not result.cite_served(f"{APP}:1 import os")


def test_cite_served_no_acepta_un_outline_como_cuerpo(tmp_path: Path) -> None:
    # 023 S1: un indice de simbolos no es el fichero.
    llm = ScriptedLlm(_tools(_read(APP, mode="outline")), json.dumps({"findings": []}))
    result = _run(
        llm,
        build_fixture_reader(tmp_path),
        options=ReviewToolsOptions(read_outline=True),
    )
    assert not result.cite_served(f"{APP}:3: needle = 1")


def test_el_registro_de_lecturas_sobrevive_a_un_lote_anterior(tmp_path: Path) -> None:
    # 023 S1: el registro lo comparte el revisor entre lotes.
    from agent_review.ports.context import Reading, ReadingRegistry

    readings = ReadingRegistry([Reading(path=APP, kind="full")])
    llm = ScriptedLlm(_tools(_read(README)), json.dumps({"findings": []}))
    result = _run(llm, build_fixture_reader(tmp_path), readings=readings)
    assert result.cite_served(f"{APP}:3: needle = 1")
    assert result.cite_served(f"{README}:1 # repo")


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


# --- Forwarding y errores tipados (023 C0b) ----------------------------------------


class RecordingReader:
    """Puerto que apunta cada argumento recibido y sirve una respuesta fija."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def read_file(
        self,
        path: str,
        *,
        ref: str = "head",
        start: int | None = None,
        end: int | None = None,
        mode: str = "full",
    ) -> ReadResult:
        self.calls.append(
            ("read_file", {"path": path, "ref": ref, "start": start, "end": end, "mode": mode})
        )
        return ReadResult(content="contenido", total_bytes=9)

    def grep(
        self,
        pattern: str,
        *,
        glob: str | None = None,
        path: str | None = None,
        ref: str = "head",
        mode: str = "literal",
        files_only: bool = False,
    ) -> ToolResult:
        self.calls.append(
            (
                "grep",
                {
                    "pattern": pattern,
                    "glob": glob,
                    "path": path,
                    "ref": ref,
                    "mode": mode,
                    "files_only": files_only,
                },
            )
        )
        return ToolResult(items=("src/app.py:1: needle",), total=1)

    def list_dir(self, path: str, *, glob: str | None = None, depth: int = 1) -> ToolResult:
        self.calls.append(("list_dir", {"path": path, "glob": glob, "depth": depth}))
        return ToolResult(items=("app.py",), total=1)


def test_cada_argumento_documentado_llega_al_adaptador() -> None:
    # R11: el adaptador puede soportar el modo y el bucle descartarlo; este test
    # fija el contrato argumento a argumento. `READ_OUTLINE` encendido es lo que
    # hace que `mode="outline"` llegue al lector (apagado se sirve `full` y se avisa).
    reader = RecordingReader()
    llm = ScriptedLlm(
        _tools(
            {
                "tool": "read_file",
                "path": APP,
                "ref": "base",
                "start": 2,
                "end": 4,
                "mode": "outline",
            },
            {
                "tool": "grep",
                "pattern": "needle",
                "glob": "**/*.py",
                "path": "src",
                "ref": "base",
                "mode": "regex",
                "files_only": True,
            },
            {"tool": "list_dir", "path": "src", "glob": "*.py", "depth": 2},
        ),
        json.dumps({"findings": []}),
    )
    _run(llm, reader, options=ReviewToolsOptions(read_outline=True))

    assert reader.calls == [
        (
            "read_file",
            {"path": APP, "ref": "base", "start": 2, "end": 4, "mode": "outline"},
        ),
        (
            "grep",
            {
                "pattern": "needle",
                "glob": "**/*.py",
                "path": "src",
                "ref": "base",
                "mode": "regex",
                "files_only": True,
            },
        ),
        ("list_dir", {"path": "src", "glob": "*.py", "depth": 2}),
    ]


def test_search_es_alias_de_grep_y_query_de_pattern() -> None:
    reader = RecordingReader()
    llm = ScriptedLlm(
        _tools({"tool": "search", "query": "needle"}),
        json.dumps({"findings": []}),
    )
    _run(llm, reader)
    assert reader.calls[0][0] == "grep"
    assert reader.calls[0][1]["pattern"] == "needle"


def test_un_tool_error_llega_al_modelo_con_su_mensaje() -> None:
    class SoloLiterales(RecordingReader):
        def grep(self, pattern: str, **kwargs: Any) -> ToolResult:
            raise ToolError("`mode=regex` no esta soportado; usa `literal`")

    llm = ScriptedLlm(
        _tools({"tool": "grep", "pattern": "needle", "mode": "regex"}),
        json.dumps({"findings": []}),
    )
    result = _run(llm, SoloLiterales())
    assert result.tool_calls == 0, "una peticion no servida no cuenta como llamada"
    assert "ERROR: `mode=regex` no esta soportado" in llm.calls[-1]["prompt"]


def test_el_flag_grep_mode_fija_el_default_y_el_modelo_puede_cambiarlo() -> None:
    # E11b: el flag decide el comportamiento por defecto; no oculta la capacidad.
    reader = RecordingReader()
    llm = ScriptedLlm(
        _tools(
            {"tool": "grep", "pattern": "needle"},
            {"tool": "grep", "pattern": "needle", "mode": "literal"},
        ),
        json.dumps({"findings": []}),
    )
    _run(llm, reader, options=ReviewToolsOptions(grep_mode="regex"))
    assert [call[1]["mode"] for call in reader.calls] == ["regex", "literal"]


# --- Aviso de corte y `outline` (023 R1) -------------------------------------------


class FixedReader:
    """Lector que devuelve un resultado fijo y apunta el `mode` recibido."""

    def __init__(
        self,
        *,
        reading: ReadResult | None = None,
        found: ToolResult | None = None,
        listing: ToolResult | None = None,
    ) -> None:
        self.reading = reading or ReadResult(content="uno\ndos", total_bytes=5000)
        self.found = found or ToolResult(items=("a.py:1: x",), total=1)
        self.listing = listing or ToolResult(items=("a.py",), total=1)
        self.modes: list[str] = []

    def read_file(self, path: str, **kwargs: Any) -> ReadResult | None:
        self.modes.append(str(kwargs.get("mode", "full")))
        return self.reading

    def grep(self, pattern: str, **kwargs: Any) -> ToolResult:
        return self.found

    def list_dir(self, path: str, **kwargs: Any) -> ToolResult:
        return self.listing


def test_el_aviso_de_corte_lleva_el_tamano_real() -> None:
    reader = FixedReader(reading=ReadResult(content="uno\ndos", total_bytes=5000, truncated=True))
    llm = ScriptedLlm(_tools({"tool": "read_file", "path": "big.py"}), json.dumps({"findings": []}))
    _run(llm, reader)
    prompt = llm.calls[-1]["prompt"]
    assert "cortado a 7 de 5000 bytes" in prompt, "7 son los bytes servidos, no los caracteres"
    assert "start/end" in prompt


def test_el_aviso_de_corte_se_calla_si_se_apaga_el_flag() -> None:
    reader = FixedReader(reading=ReadResult(content="uno", total_bytes=5000, truncated=True))
    llm = ScriptedLlm(_tools({"tool": "read_file", "path": "big.py"}), json.dumps({"findings": []}))
    _run(llm, reader, options=ReviewToolsOptions(truncation_notice=False))
    assert "cortado" not in llm.calls[-1]["prompt"]


def test_el_aviso_de_corte_declara_mostrando_n_de_m_en_grep() -> None:
    reader = FixedReader(
        found=ToolResult(items=("a.py:1: x", "b.py:2: y"), total=340, truncated=True)
    )
    llm = ScriptedLlm(_tools({"tool": "grep", "pattern": "x"}), json.dumps({"findings": []}))
    _run(llm, reader)
    prompt = llm.calls[-1]["prompt"]
    assert "mostrando 2 de 340" in prompt
    assert "path/glob/files_only" in prompt


def test_el_aviso_de_corte_declara_mostrando_n_de_m_en_list_dir() -> None:
    reader = FixedReader(listing=ToolResult(items=("a.py",), total=9, truncated=True))
    llm = ScriptedLlm(_tools({"tool": "list_dir", "path": "src"}), json.dumps({"findings": []}))
    _run(llm, reader)
    prompt = llm.calls[-1]["prompt"]
    assert "mostrando 1 de 9" in prompt
    assert "glob/depth" in prompt


def test_un_corte_del_tope_del_lote_tambien_se_anuncia() -> None:
    reader = FixedReader(reading=ReadResult(content="x" * 40, total_bytes=40))
    llm = ScriptedLlm(_tools({"tool": "read_file", "path": "big.py"}), json.dumps({"findings": []}))
    _run(llm, reader, limits=ContextLimits(max_bytes=10))
    assert "tope del lote a 10 bytes" in llm.calls[-1]["prompt"]


def test_outline_apagado_sirve_full_y_lo_avisa() -> None:
    reader = FixedReader()
    llm = ScriptedLlm(
        _tools({"tool": "read_file", "path": "a.py", "mode": "outline"}),
        json.dumps({"findings": []}),
    )
    _run(llm, reader, options=ReviewToolsOptions(read_outline=False))
    assert reader.modes == ["full"]
    assert "outline esta desactivado" in llm.calls[-1]["prompt"]


def test_outline_encendido_llega_al_lector() -> None:
    reader = FixedReader()
    llm = ScriptedLlm(
        _tools({"tool": "read_file", "path": "a.py", "mode": "outline"}),
        json.dumps({"findings": []}),
    )
    _run(llm, reader, options=ReviewToolsOptions(read_outline=True))
    assert reader.modes == ["outline"]
    assert "desactivado" not in llm.calls[-1]["prompt"]


def test_un_mode_de_read_file_desconocido_es_error() -> None:
    reader = FixedReader()
    llm = ScriptedLlm(
        _tools({"tool": "read_file", "path": "a.py", "mode": "toc"}),
        json.dumps({"findings": []}),
    )
    result = _run(llm, reader)
    assert result.tool_calls == 0
    assert reader.modes == []
    assert "`mode=toc` no existe" in llm.calls[-1]["prompt"]


def test_las_operaciones_se_cuentan_por_herramienta_y_modo(tmp_path: Path) -> None:
    # 023 M1: `operations` dice cuantas veces el modelo pidio cada modo; es lo que
    # permite saber si la palanca (`outline`, `regex`) se usa de verdad.
    llm = ScriptedLlm(
        _tools(
            _grep("needle"),
            {"tool": "grep", "pattern": "needle", "mode": "regex"},
            _read(APP),
            _read(APP, start=1, end=2),
            _read(APP, mode="outline"),
            {"tool": "list_dir", "path": "src"},
        ),
        json.dumps({"findings": []}),
    )
    result = _run(
        llm,
        build_fixture_reader(tmp_path),
        limits=ContextLimits(calls_per_turn=6),
        options=ReviewToolsOptions(read_outline=True),
    )

    assert result.operations == {
        "grep:literal": 1,
        "grep:regex": 1,
        "list_dir": 1,
        "read_file:full": 1,
        "read_file:outline": 1,
        "read_file:range": 1,
    }


def test_un_grep_sin_coincidencias_tambien_cuenta_como_uso_del_modo() -> None:
    # Gasto la peticion aunque no encuentre nada: esconderla maquillaria el uso.
    reader = FixedReader(found=ToolResult(items=()))
    llm = ScriptedLlm(_tools({"tool": "grep", "pattern": "nada"}), json.dumps({"findings": []}))

    result = _run(llm, reader)

    assert result.operations == {"grep:literal": 1}


def test_outline_apagado_cuenta_lo_que_de_verdad_se_sirvio() -> None:
    # Cae a `full`: el modelo no recibio un indice, asi que no se anota `outline`.
    reader = FixedReader()
    llm = ScriptedLlm(
        _tools({"tool": "read_file", "path": "a.py", "mode": "outline"}),
        json.dumps({"findings": []}),
    )

    result = _run(llm, reader, options=ReviewToolsOptions(read_outline=False))

    assert result.operations == {"read_file:full": 1}


class _UsageLlm:
    """Devuelve respuestas con `usage` distinto por turno (024 E9)."""

    def __init__(self, *replies: LlmReply) -> None:
        self._replies = list(replies)
        self.calls = 0

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        json_mode: str | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> LlmReply:
        del system, prompt, max_tokens, json_mode, response_schema
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return reply


def _turn(text: str, *, prompt: int, completion: int, reasoning: int, cached: int | None):
    return LlmReply(
        text=text,
        prompt_tokens=prompt,
        completion_tokens=completion,
        reasoning_tokens=reasoning,
        cached_tokens=cached,
    )


def test_el_bucle_separa_prompt_completion_razonamiento_y_cache(tmp_path: Path) -> None:
    # 024 E9: `usage` sigue siendo la suma para el presupuesto, pero el informe
    # necesita ver que parte es razonamiento y cuanto descuenta el proveedor.
    llm = _UsageLlm(
        _turn(_tools(_read(APP)), prompt=100, completion=20, reasoning=8, cached=40),
        _turn(json.dumps({"findings": []}), prompt=120, completion=30, reasoning=10, cached=50),
    )

    result = _run(llm, build_fixture_reader(tmp_path))

    assert (result.prompt_tokens, result.completion_tokens, result.reasoning_tokens) == (
        220,
        50,
        18,
    )
    assert result.usage == 270
    assert result.cached_tokens == 90


def test_un_turno_sin_cache_deja_la_medida_en_none(tmp_path: Path) -> None:
    # Sumar solo los turnos que lo reportan daria un numero incompleto con aspecto
    # de medido: `None` es la respuesta honesta (024 E3).
    llm = _UsageLlm(
        _turn(_tools(_read(APP)), prompt=100, completion=20, reasoning=8, cached=40),
        _turn(json.dumps({"findings": []}), prompt=120, completion=30, reasoning=10, cached=None),
    )

    result = _run(llm, build_fixture_reader(tmp_path))

    assert result.cached_tokens is None


def test_sin_turnos_no_hay_medida_de_cache() -> None:
    result = LoopResult()

    assert result.cached_tokens is None


def test_read_state_aparece_tras_el_tool_result_y_no_en_el_prompt_inicial(
    tmp_path: Path,
) -> None:
    # 023 S1: el bloque va al final del ultimo mensaje de usuario, no en el inicial.
    llm = ScriptedLlm(_tools(_read(APP)), json.dumps({"findings": []}))
    _run(llm, build_fixture_reader(tmp_path), options=ReviewToolsOptions(read_state=True))
    first, last = llm.calls[0]["prompt"], llm.calls[-1]["prompt"]
    assert "LECTURAS" not in first
    assert "LECTURAS (esta review):" in last
    assert last.index("</tool_result>") < last.index("LECTURAS")


def test_read_state_apagado_no_anade_bloque(tmp_path: Path) -> None:
    llm = ScriptedLlm(_tools(_read(APP)), json.dumps({"findings": []}))
    _run(llm, build_fixture_reader(tmp_path))
    assert "LECTURAS" not in llm.calls[-1]["prompt"]


def test_read_state_declara_tipo_y_ref_de_cada_lectura(tmp_path: Path) -> None:
    llm = ScriptedLlm(
        _tools(
            _grep("needle"),
            _read(APP, start=1, end=2),
            _read(APP, mode="outline"),
            _read(APP, ref="base"),
        ),
        json.dumps({"findings": []}),
    )
    _run(
        llm,
        build_fixture_reader(tmp_path),
        limits=ContextLimits(calls_per_turn=4),
        options=ReviewToolsOptions(read_state=True, read_outline=True),
    )
    prompt = llm.calls[-1]["prompt"]
    assert f"- {APP} (head): coincidencias" in prompt
    assert f"- {APP} (head): lineas 1-2" in prompt
    assert f"- {APP} (head): indice" in prompt
    assert f"- {APP} (base): completo" in prompt


def test_read_state_no_crece_sin_limite(tmp_path: Path) -> None:
    # 023 S1: el bloque muestra las ultimas lecturas y declara cuantas omite.
    reader = build_fixture_reader(tmp_path)
    for index in range(25):
        (tmp_path / f"f{index}.py").write_text("x = 1\n", encoding="utf-8")
    llm = ScriptedLlm(
        _tools(*(_read(f"f{index}.py") for index in range(25))),
        json.dumps({"findings": []}),
    )
    _run(
        llm,
        reader,
        limits=ContextLimits(calls_per_turn=25, max_calls=30, max_turns=5),
        options=ReviewToolsOptions(read_state=True),
    )
    prompt = llm.calls[-1]["prompt"]
    assert prompt.count("\n- ") == 20
    assert "5 lecturas anteriores omitidas" in prompt
