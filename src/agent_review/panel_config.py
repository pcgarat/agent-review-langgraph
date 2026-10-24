"""Diales que el panel publica (027, ADR-0031).

El valor inicial de un control es el que `Settings` ya resolvió. El documento
guardado solo contiene las claves que se apartan de ese valor. Restaurar una
clave es quitarla del documento.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, TypeVar, get_args, get_origin

from agent_review.domain.models import FindingAction
from agent_review.policy.engine import PolicyError, reject_if_cap_raised
from agent_review.settings import Dial, Secret, Settings

# El techo se puede entregar ya resuelto o como proveedor: el `web` solo puede
# leer `autonomy.yaml` si el despliegue monta `config/`, asi que se resuelve
# unicamente cuando el formulario pide un override distinto del inicial.
Ceiling = str | Callable[[], str]

# Orden de las secciones en la página. Un dial con otra sección no se pinta.
SECTIONS: tuple[str, ...] = (
    "Presupuesto y lotes",
    "Verificador",
    "Bucle de lectura",
    "Política",
)


class ConfigError(ValueError):
    """Un documento que el panel no puede publicar. `field` es el nombre del dial."""

    def __init__(self, field: str, detail: str) -> None:
        self.field = field
        super().__init__(f"{field}: {detail}")


class RevisionConflict(ConfigError):
    """Otro guardado entró antes. `current` es la revisión que sigue vigente."""

    def __init__(self, current: int) -> None:
        self.current = current
        super().__init__("revision", f"la revisión vigente es {current}")


class StaleForm(ConfigError):
    """El formulario se pintó con otros valores iniciales que los de este proceso.

    Un despliegue puede cambiar el default de un dial con la página abierta. Guardar
    entonces publicaría como override lo que el operador nunca eligió (o rechazaría
    un dial nuevo por «falta en el formulario»), así que el guardado se rechaza y el
    operador recarga.
    """

    def __init__(self, field: str) -> None:
        super().__init__(field, "la página tiene valores iniciales de otra versión")


@dataclass(frozen=True)
class DialField:
    name: str
    section: str
    help: str


def catalog() -> tuple[DialField, ...]:
    """Diales en el orden de `Settings`. Un secreto con sección es un error."""
    found: list[DialField] = []
    for name, info in Settings.model_fields.items():
        dial = _meta(info, Dial)
        secret = _meta(info, Secret)
        if dial is not None and secret is not None:
            raise RuntimeError(f"{name} declara Dial y Secret")
        if dial is None:
            continue
        if dial.section not in SECTIONS:
            raise RuntimeError(f"{name} tiene sección desconocida: {dial.section!r}")
        found.append(DialField(name=name, section=dial.section, help=dial.help))
    return tuple(found)


def lock_names(settings: Settings) -> frozenset[str]:
    """Nombres de `PANEL_CONFIG_LOCK`. Uno que no sea dial aborta."""
    known = {item.name for item in catalog()}
    names = [part.strip() for part in settings.panel_config_lock.split(",") if part.strip()]
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ConfigError("panel_config_lock", f"nombres fuera del catálogo: {', '.join(unknown)}")
    return frozenset(names)


def initial_values(settings: Settings) -> dict[str, object]:
    return {item.name: getattr(settings, item.name) for item in catalog()}


def document_for(
    settings: Settings,
    submitted: Mapping[str, object],
    *,
    autonomy_ceiling: Ceiling,
    painted: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Formulario entero → overrides. Solo claves distintas del valor inicial.

    `painted` son los valores iniciales con los que se pintó el formulario. Sin
    ellos, el diff se hace contra `Settings` del proceso: si un despliegue cambia un
    default con una página abierta, el primer guardado publicaría como override el
    valor viejo, y un dial nuevo daría «falta en el formulario». Con ellos, un
    desfase se rechaza para que el operador recargue.

    Las claves bloqueadas se aceptan si llegan con su valor inicial: el navegador
    omite los controles `disabled`, y rechazarlas dejaría sin guardar cualquier otro
    dial cuando el despliegue añade un candado sobre un override ya publicado. Un
    valor distinto sigue rechazándose.
    """
    fields = {item.name: Settings.model_fields[item.name] for item in catalog()}
    unknown = sorted(set(submitted) - set(fields))
    if unknown:
        raise ConfigError(unknown[0], "no es un dial")
    initial = initial_values(settings)
    locked = lock_names(settings)
    if painted is not None:
        # Antes que «falta en el formulario»: un dial que un despliegue añadió o
        # retiró con la página abierta se resuelve recargando (409), no culpando al
        # formulario (400). Un candado con valor distinto sigue siendo 400.
        drift = sorted(set(painted) ^ set(initial))
        drift += sorted(
            name
            for name, value in painted.items()
            if name in initial and _differs(name, initial[name], value)
        )
        if drift:
            raise StaleForm(drift[0])
    missing = sorted(set(initial) - set(submitted) - locked)
    if missing:
        raise ConfigError(missing[0], "falta en el formulario")
    document: dict[str, object] = {}
    for name, raw in submitted.items():
        value = _coerce(name, raw, fields[name].annotation)
        if name == "max_autonomy_override" and isinstance(value, str) and value != initial[name]:
            try:
                reject_if_cap_raised(_resolve_ceiling(autonomy_ceiling), value)
            except PolicyError as exc:
                raise ConfigError(name, str(exc)) from exc
        if name in locked and value != initial[name]:
            raise ConfigError(name, "lo fija el despliegue")
        if value != initial[name]:
            document[name] = value
    return document


def _resolve_ceiling(ceiling: Ceiling) -> str:
    return ceiling() if callable(ceiling) else ceiling


def _differs(name: str, current: object, painted: object) -> bool:
    """¿El valor pintado difiere del inicial de este proceso?

    Se comparan ya normalizados por el tipo del campo: el formulario manda texto y
    `Settings` trae `int`/`bool`, así que un `10` y un `"10"` son el mismo valor.
    """
    try:
        return _coerce(name, painted, Settings.model_fields[name].annotation) != current
    except ConfigError:
        return True


def apply_document(settings: Settings, document: Mapping[str, object]) -> Settings:
    """Settings efectivos. Una clave bloqueada o desconocida no se aplica."""
    allowed = {item.name for item in catalog()} - lock_names(settings)
    update = {key: value for key, value in document.items() if key in allowed}
    if not update:
        return settings
    return settings.model_copy(update=update)


def choose_document(
    *,
    published: Mapping[str, object],
    checkpoint: Mapping[str, object] | None,
    repin: bool,
) -> dict[str, object]:
    """Qué foto monta esta review.

    Sin hilo, o con `repin` (reintento del panel), manda lo publicado. Con hilo,
    manda la foto que el hilo guardó. Un hilo anterior a esta iniciativa no tiene
    foto: se monta con `Settings` (documento vacío).
    """
    if checkpoint is None or repin:
        return dict(published)
    raw = checkpoint.get("dial_snapshot")
    if not isinstance(raw, dict):
        return {}
    overrides = raw.get("overrides")
    if not isinstance(overrides, dict):
        return {}
    return dict(overrides)


def snapshot_revision(values: Mapping[str, object] | None) -> int | None:
    """Revisión fijada en el estado del hilo, si el checkpoint la trae."""
    if not values:
        return None
    snapshot = values.get("dial_snapshot")
    if not isinstance(snapshot, dict):
        return None
    revision = snapshot.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int):
        return None
    return revision


def snapshot_of(revision: int, overrides: Mapping[str, object]) -> dict[str, object]:
    return {"revision": revision, "overrides": dict(overrides)}


def pinned_snapshot(
    *,
    published_revision: int,
    published: Mapping[str, object],
    checkpoint: Mapping[str, object] | None,
    repin: bool,
) -> dict[str, object]:
    """Foto que se escribe en el estado y con la que se monta el grafo."""
    overrides = choose_document(published=published, checkpoint=checkpoint, repin=repin)
    if checkpoint is not None and not repin:
        raw = checkpoint.get("dial_snapshot")
        revision = raw.get("revision") if isinstance(raw, dict) else None
        return snapshot_of(revision if isinstance(revision, int) else 0, overrides)
    return snapshot_of(published_revision, overrides)


_Marker = TypeVar("_Marker", Dial, Secret)


def _meta(field_info: object, kind: type[_Marker]) -> _Marker | None:
    metadata = getattr(field_info, "metadata", ())
    return next((item for item in metadata if isinstance(item, kind)), None)


def _coerce(name: str, value: object, annotation: object) -> object:
    if annotation is bool:
        return _coerce_bool(name, value)
    if annotation is int:
        return _coerce_int(name, value)
    if annotation is str or get_origin(annotation) is Literal:
        return _coerce_str(name, value, annotation)
    raise ConfigError(name, "tipo no publicable")


def _coerce_bool(name: str, value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"true", "1", "yes", "on"}:
            return True
        if token in {"false", "0", "no", "off"}:
            return False
    raise ConfigError(name, "se espera un booleano")


def _coerce_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConfigError(name, "se espera un entero")
    text = str(value).strip()
    if not text:
        raise ConfigError(name, "se espera un entero")
    try:
        number = int(text)
    except ValueError:
        raise ConfigError(name, "se espera un entero") from None
    if number < 0:
        raise ConfigError(name, "no puede ser negativo")
    return number


def _coerce_str(name: str, value: object, annotation: object) -> str:
    if not isinstance(value, str):
        raise ConfigError(name, "se espera un texto")
    text = value.strip()
    if get_origin(annotation) is Literal and text not in get_args(annotation):
        allowed = ", ".join(str(item) for item in get_args(annotation))
        raise ConfigError(name, f"debe ser uno de: {allowed}")
    if name == "max_autonomy_override" and text:
        try:
            FindingAction(text)
        except ValueError:
            raise ConfigError(name, f"valor de autonomía desconocido: {text!r}") from None
    return text
