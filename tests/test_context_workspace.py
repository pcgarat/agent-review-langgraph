"""L9: `WorkspaceContextReader`, el puerto servido por el runner (016 W5, W8).

El contrato comun vive en `tests/test_context_ports.py`. Aqui se fijan las reglas
propias del adaptador HTTP: apertura idempotente, reapertura unica ante un `410`,
traduccion de errores del runner a `TransientError`/`PermanentError` y el envio del
bearer.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from context_fakes import (
    APP,
    APP_TEXT,
    BASE,
    ENV,
    README,
    build_workspace_reader,
    runner_handler,
)

from agent_review.adapters.context_workspace import (
    ContextError,
    ContextUnavailable,
    WorkspaceContextReader,
)


def _counting(events: list[str], inner=None) -> Any:
    base = inner or runner_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        events.append(f"{request.method} {request.url.path}")
        return base(request)

    return handler


def test_abre_el_workspace_una_sola_vez_por_lote() -> None:
    events: list[str] = []
    reader = build_workspace_reader(_counting(events))
    reader.read_file(APP)
    reader.read_file(README)
    reader.grep("needle")
    reader.list_dir("src")
    assert events.count("POST /workspaces") == 1, "idempotente por (repo, head_sha) (W6)"


def test_envia_repo_head_y_base_al_abrir() -> None:
    captured: dict[str, Any] = {}
    inner = runner_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/workspaces" and request.method == "POST":
            import json

            captured.update(json.loads(request.content))
        return inner(request)

    build_workspace_reader(handler).read_file(APP)
    assert captured == {"repo": "cqm/x", "head_sha": "head1", "base_sha": BASE}


def test_se_reabre_una_vez_si_el_runner_lo_perdio() -> None:
    files = {"n": 0}
    inner = runner_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/file"):
            files["n"] += 1
            if files["n"] == 1:
                return httpx.Response(410, json={"detail": "workspace perdido"})
        return inner(request)

    reader = build_workspace_reader(handler)
    assert reader.read_file(APP) == APP_TEXT
    assert files["n"] == 2, "una reapertura, un reintento (W8)"


def test_un_404_de_fichero_es_fichero_ausente_sin_reabrir() -> None:
    # El `404` de `file` es un fichero que no existe en el commit: no es un
    # workspace perdido (eso es `410`), asi que no gasta la unica reapertura.
    posts: list[str] = []
    inner = runner_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/workspaces" and request.method == "POST":
            posts.append(request.url.path)
        if request.url.path.endswith("/file"):
            return httpx.Response(404, json={"detail": "no existe"})
        return inner(request)

    assert build_workspace_reader(handler).read_file(APP) is None
    assert len(posts) == 1, "solo la apertura inicial"


def test_un_fichero_ausente_no_gasta_la_reapertura_de_una_perdida_real() -> None:
    # Primero el modelo pide una ruta inventada (`404`); despues el runner pierde
    # el workspace (`410`). La reapertura sigue disponible para la perdida real.
    posts: list[str] = []
    lost = {"value": False}
    inner = runner_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/workspaces" and request.method == "POST":
            posts.append(request.url.path)
            lost["value"] = False
        if request.url.path.endswith("/file"):
            if request.url.params.get("path") == "src/inventado.py":
                lost["value"] = True
                return httpx.Response(404, json={"detail": "no existe"})
            if lost["value"]:
                return httpx.Response(410, json={"detail": "workspace perdido"})
        return inner(request)

    reader = build_workspace_reader(handler)
    assert reader.read_file("src/inventado.py") is None
    assert reader.read_file(APP) == APP_TEXT
    assert len(posts) == 2, "apertura inicial y una reapertura por la perdida real"


def test_si_el_workspace_sigue_perdido_en_read_file_es_error_permanente() -> None:
    # Tras la reapertura, un `410` que persiste sube para que el lector degrade al
    # VCS (W8); no se confunde con un fichero ausente.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/file"):
            return httpx.Response(410, json={"detail": "workspace perdido"})
        return runner_handler()(request)

    with pytest.raises(ContextError, match="workspace no disponible"):
        build_workspace_reader(handler).read_file(APP)


def test_si_el_workspace_sigue_perdido_en_grep_es_error_permanente() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/grep"):
            return httpx.Response(410, json={"detail": "workspace perdido"})
        return runner_handler()(request)

    with pytest.raises(ContextError, match="workspace no disponible"):
        build_workspace_reader(handler).grep("needle")


def test_un_5xx_es_transitorio() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/grep"):
            return httpx.Response(503, json={"detail": "clonando"})
        return runner_handler()(request)

    with pytest.raises(ContextUnavailable):
        build_workspace_reader(handler).grep("needle")


def test_un_429_es_transitorio() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tree"):
            return httpx.Response(429, json={"detail": "demasiadas peticiones"})
        return runner_handler()(request)

    with pytest.raises(ContextUnavailable):
        build_workspace_reader(handler).list_dir("src")


def test_un_4xx_es_permanente_con_detalle() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/workspaces" and request.method == "POST":
            return httpx.Response(422, json={"detail": "repo invalido"})
        return runner_handler()(request)

    with pytest.raises(ContextError, match="repo invalido"):
        build_workspace_reader(handler).read_file(APP)


def test_un_4xx_sin_json_da_el_texto_plano() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/workspaces" and request.method == "POST":
            return httpx.Response(422, text="no es json")
        return runner_handler()(request)

    with pytest.raises(ContextError, match="no es json"):
        build_workspace_reader(handler).read_file(APP)


def test_sin_workspace_id_es_error_permanente() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/workspaces" and request.method == "POST":
            return httpx.Response(200, json={})
        return runner_handler()(request)

    with pytest.raises(ContextError, match="workspace_id"):
        build_workspace_reader(handler).read_file(APP)


def test_sin_red_es_transitorio() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ContextUnavailable):
        build_workspace_reader(handler).read_file(APP)


def test_la_ruta_vetada_por_el_runner_devuelve_none() -> None:
    reader = build_workspace_reader(runner_handler(extra_denied=[README]))
    assert reader.read_file(README) is None


def test_una_ruta_vetada_por_el_denylist_local_no_llega_al_runner() -> None:
    events: list[str] = []
    reader = WorkspaceContextReader(
        base_url="http://runner:8099",
        repo="cqm/x",
        head_sha="head1",
        denylist=(".env*",),
        client=httpx.Client(transport=httpx.MockTransport(_counting(events))),
    )
    assert reader.read_file(ENV) is None
    assert events == [], "el veto local corta antes de la peticion"


def test_envia_el_bearer_si_hay_token() -> None:
    seen: list[str | None] = []
    inner = runner_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return inner(request)

    build_workspace_reader(handler, token="secreto").read_file(APP)
    assert seen and all(value == "Bearer secreto" for value in seen)


def test_sin_token_no_envia_authorization() -> None:
    seen: list[str | None] = []
    inner = runner_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return inner(request)

    build_workspace_reader(handler).read_file(APP)
    assert seen and all(value is None for value in seen)


def test_envia_el_rango_y_la_ref() -> None:
    captured: dict[str, str] = {}
    inner = runner_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/file"):
            captured.update(dict(request.url.params))
        return inner(request)

    build_workspace_reader(handler).read_file(APP, ref="base", start=2, end=3)
    assert captured == {"path": APP, "ref": "base", "start": "2", "end": "3"}


def test_grep_acota_por_glob() -> None:
    reader = build_workspace_reader()
    assert reader.grep("needle", glob="**/*.py") == [
        f"{APP}:3: needle = 1",
        f"{APP}:4: print(needle)",
    ]
    assert reader.grep("needle", glob="**/*.md") == []


def test_sin_pattern_no_pide_nada() -> None:
    sent: list[str] = []
    reader = build_workspace_reader(_counting(sent))
    assert reader.grep("") == []
    assert sent == []


def test_el_listado_del_runner_se_normaliza() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tree"):
            return httpx.Response(200, json={"entries": ["b.py", "a.py", 7]})
        return runner_handler()(request)

    assert build_workspace_reader(handler).list_dir("src") == ["b.py", "a.py"]


def test_grep_ignora_coincidencias_no_textuales() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/grep"):
            return httpx.Response(200, json={"matches": ["a.py:1: x", 5]})
        return runner_handler()(request)

    assert build_workspace_reader(handler).grep("x") == ["a.py:1: x"]
