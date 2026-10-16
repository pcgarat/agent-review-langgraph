# ADR-0018 — El `sandbox-runner` también sirve lecturas del repo desde un clon *bare*

- **Estado:** Propuesto
- **Fecha:** 2026-10-16

## Contexto

El verificador adversarial ([009](../iniciativas/009-verificador-adversarial/spec.md))
y el revisor con herramientas ([014](../iniciativas/014-revisor-con-herramientas/spec.md))
necesitan leer ficheros fuera del diff y hacer `grep` sobre el árbol completo a
`head_sha`. `VcsPort.get_file` solo lee fichero a fichero, cada lectura lanza un
proceso `twg` y no permite buscar en el árbol.

Un `grep` completo exige un clon. Según [ADR-0011](0011-sandbox-runner-aislado.md),
el único proceso con credencial de clonado es el `sandbox-runner`, y su alcance hoy es
solo verificar parches.

## Decisión

- El `sandbox-runner` amplía su alcance: **verifica parches y sirve lecturas**. Lo
  hace con endpoints `/workspaces` para abrir, leer un fichero, hacer `grep`, listar
  el árbol y borrar. El contrato está en
  [016 §5.3](../iniciativas/016-lector-de-contexto/spec.md).
- **Clon *bare*, sin *checkout* ni contenedor.** Las lecturas salen de los objetos de
  git (`git show`, `git grep`, `git ls-tree`) a `head_sha` y `base_sha`. Al no haber
  árbol de trabajo, no se ejecutan *hooks* ni filtros y ningún *symlink* puede apuntar
  fuera. El clon vive fuera de lo que se monta en los contenedores de `/verify`.
- **Apertura idempotente por `(repo, head_sha)`**, TTL de 1800 s y como máximo 4
  workspaces abiertos. Las lecturas tienen su propio límite de concurrencia, separado
  del de `/verify`. El estado vive en la memoria del runner.
- **Rutas vetadas en el runner** (`context_denylist:` del perfil, con default
  `.env*`, `*.pem`, `*.key` y `**/secrets/**`), no en el adaptador.
- El worker de reviews gana acceso HTTP al runner, solo para `/workspaces`, con el
  mismo bearer.

## Consecuencias

- El `grep` del árbol completo cuesta una sola clonación por review, no un proceso
  `twg` por fichero.
- El runner pasa a estar en la ruta de la review, no solo en la del fix. Si está
  caído, los lectores se degradan a `VcsContextReader` y la review no se cae.
- El runner, que ejecuta código del PR durante `setup_network`, ahora también sirve el
  contenido que leen los modelos. Si se compromete, ya es un incidente de ADR-0011.
  El veredicto no toca la severidad, así que el daño queda acotado.
- Un reinicio del runner pierde los workspaces abiertos. El adaptador reabre una vez.

## Alternativas descartadas

- **Clonar en el worker.** Daría credencial de clonado, git y disco al único proceso
  que publica en el PR.
- **API de búsqueda de código del VCS.** No está en `VcsPort`, indexa con retraso y
  suele cubrir la rama por defecto, no un `head_sha` recién empujado.
- **Matiz en ADR-0011.** Ampliar el alcance del runner no es una corrección menor, y
  un ADR aceptado no se reescribe.
