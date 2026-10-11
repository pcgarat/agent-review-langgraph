from __future__ import annotations

from pathlib import Path

from agent_review.profile import fix_recipes, load_profile, validate_profile
from agent_review.settings import Settings

_PROFILE = Path("config/repo_profile.emotivoo-v2.yaml")


def test_perfil_versionado_cumple_el_contrato() -> None:
    import yaml

    profile = yaml.safe_load(_PROFILE.read_text(encoding="utf-8"))
    assert validate_profile(profile) == [], "el perfil de produccion debe ser valido"


def test_perfil_versionado_declara_las_claves_de_criticas() -> None:
    import yaml

    profile = yaml.safe_load(_PROFILE.read_text(encoding="utf-8"))
    # La barrera de ingesta (B4) depende de `expected_tools`; sin el, la review
    # degradaria en silencio. El orden de verificacion vive en `verify`.
    assert profile["expected_tools"], "sin expected_tools la barrera de ingesta se rompe"
    assert profile["verify"], "sin verify el sandbox no sabe que comprobar"
    assert "targeted" in profile["checks"], "la review dirigida necesita checks.targeted"
    assert profile["sandbox"]["timeout_s"] > 0


def test_clave_desconocida_es_un_problema_de_contrato() -> None:
    problems = validate_profile({"repo": "r", "expected_tool": ["phpstan"]})
    assert any("desconocida" in problem for problem in problems)


def test_tipo_equivocado_es_un_problema_de_contrato() -> None:
    assert validate_profile({"repo": "r", "expected_tools": "phpstan"})
    assert validate_profile({"repo": "r", "checks": ["typecheck"]})
    assert validate_profile({"repo": "r", "setup": [1, 2]})


def test_falta_repo_es_un_problema_de_contrato() -> None:
    assert validate_profile({"expected_tools": ["phpstan"]})


def test_perfil_vacio_cumple_aunque_sea_degradado() -> None:
    # `{}` es "sin perfil", no un error de esquema; la degradacion la decide el
    # arranque (build_reviewers cae a null), no el validador.
    assert validate_profile({}) == ["falta 'repo' (identidad del perfil)"]


def test_load_profile_registra_los_problemas(tmp_path: Path, caplog) -> None:
    profile = tmp_path / "p.yaml"
    profile.write_text("repo: r\ntypo: 1\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        loaded = load_profile(Settings(repo_profile_config=str(profile)))
    assert loaded["repo"] == "r"
    assert any("clave desconocida" in record.message for record in caplog.records)


def test_fix_recipes_manda_el_perfil(tmp_path: Path) -> None:
    profile = tmp_path / "p.yaml"
    profile.write_text("repo: r\nverify: [targeted, typecheck]\n", encoding="utf-8")
    settings = Settings(repo_profile_config=str(profile), fix_recipes="lint,tests")
    assert fix_recipes(settings) == ["targeted", "typecheck"]


def test_fix_recipes_cae_al_env_sin_perfil(tmp_path: Path) -> None:
    settings = Settings(
        repo_profile_config=str(tmp_path / "no-existe.yaml"), fix_recipes="lint,tests"
    )
    assert fix_recipes(settings) == ["lint", "tests"]


def test_fix_recipes_prioriza_la_inyeccion_explicita(tmp_path: Path) -> None:
    profile = tmp_path / "p.yaml"
    profile.write_text("repo: r\nverify: [targeted]\n", encoding="utf-8")
    settings = Settings(repo_profile_config=str(profile))
    assert fix_recipes(settings, names=["typecheck"]) == ["typecheck"]
