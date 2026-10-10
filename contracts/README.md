Última modificación: 2026-10-10 | Creado: 2026-10-10

# Contratos

Contratos versionados copiados del ecosistema `ce-code-review`. **No** se invoca su
runtime (es una skill de prompt, no una API): se copia el contrato y se reimplementa el
agente contra él.

## `findings-schema.json`

Espejo del modelo `reviews.domain.models.Finding`. Si cambia el modelo Pydantic, el
esquema debe cambiar en el mismo commit (hay un test que compara ambos).

Anclas importantes del contrato:

- `confidence`: valor **anclado** a 0/25/50/75/100. Evita falsa precisión ("87%").
- `evidence`: al menos un elemento. Un hallazgo sin evidencia citada no entra.
- `autofix_class`: `gated_auto` (candidato a arreglo), `manual` (decisión humana),
  `advisory` (solo informar).
- `protected_subject`: si no es `null`, el hallazgo nunca es automático.
- `pre_existing`: distingue deuda previa de la introducida por el PR.

La política (`config/autonomy.yaml`) se apoya en estos campos, no en prosa libre.

## Regenerar

```bash
uv run python -c "import json; from reviews.domain.models import Finding; \
  print(json.dumps(Finding.model_json_schema(), indent=2, ensure_ascii=False))"
```

Ajusta a mano `confidence.enum` si el generador no propaga el validador anclado.
