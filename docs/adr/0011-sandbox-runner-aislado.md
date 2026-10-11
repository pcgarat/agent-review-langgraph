# ADR-0011 — Verificación en un `sandbox-runner` aislado, rootless y con la imagen por digest

- **Estado:** Aceptado. Desacople fuerte (E7) pendiente.
- **Fecha:** 2026-10-11

## Contexto

Verificar un fix significa ejecutar código de un PR de terceros. El objetivo es
«igual que en el CI de ese repo», no «igual que en mi portátil». `emotivoo-v2` ya
ejecuta todo con `make` y `EXEC_PHP=""` dentro de una imagen de CI dedicada.

## Decisión

- **Servicio aparte (`sandbox-runner`).** El worker **nunca** toca un socket de
  Docker: habla HTTP interno (`POST /verify`, con un bearer compartido). El runner usa
  su **propio demonio Docker rootless**. No publica puertos y no recibe las
  credenciales de `twg`.
- **El ciclo de vida entero vive en el runner:** resolver la imagen → clonar
  `repo@head_sha` con un token de **solo lectura** → `git apply --check` + `apply` →
  `setup_network` con red (`composer install --no-scripts`) → `setup` y checks con
  `--network none` → **destruir siempre** en un `finally`.
- **Imagen por digest.** `ci_image_digest: sha256:…` en el perfil. Sin digest, o con
  un tag mutable, el runner rechaza la petición (`409`) y el fix queda
  `NOT_VERIFIABLE`.
- **Solo recetas declaradas.** Argv con `shlex.split`, nunca `shell=True`. Los
  `targets` de la receta dirigida pasan por una *allowlist* (`tests/**.php`, sin `..`
  ni rutas absolutas).
- **Contenedor endurecido:** `--cap-drop ALL`, `no-new-privileges`, usuario no root,
  2 CPU y 4 GB por defecto.
- **Síncrono e idempotente.** `POST /verify` bloquea; el `run_key` devuelve el informe
  previo si se repite y rechaza uno que ya esté en curso. Concurrencia 1.
- **Orden de verificación:** el `verify` del perfil, hoy
  `targeted → typecheck → arch → lint → tests`. Para al primer fallo.
  - `targeted` da la señal barata del área tocada. Sin targets explícitos, el runner
    los calcula del fichero que toca el parche (`recipes.discover_targets`):
    `src/…/Clase.php` → `tests/**/ClaseTest.php`, y un test tocado se ejecuta tal
    cual. Sin coincidencias, se salta.
  - **La suite completa (`tests`) cierra siempre**: un fix solo es `VERIFIED` si pasa
    entera.
- **Red solo para descargar.** `setup_network` (con red) contiene solo lo que
  descarga: `composer install --no-scripts` e `importmap:install`. Este último arranca
  el kernel, es decir, código del PR con red: riesgo asumido porque sin él fallan los
  tests funcionales. `cache:clear` y `assets:install` van en `setup`, sin red.

## Consecuencias

- Si el worker queda comprometido, no tiene socket de Docker ni credenciales de
  clonado o de registro.
- Si no hay Docker rootless, el runner no arranca y no hay modo «socket del host»
  (R7).
- **El worker es secuencial:** una verificación de ~20 min bloquea las demás reviews.
  E7 pide una cola o consumidor de fixes aparte; el adaptador síncrono no lo cumple.
  Propuesta en [spec de E7](../iniciativas/004-fase3-e7-desacople/spec.md).
- El idempotente de `run_key` vive en memoria del runner y no sobrevive a su reinicio.
- La precondición P6 (revisar con seguridad la heurística de tema protegido) bloquea
  el smoke real del runner.

## Alternativas descartadas

- **`POST /runs` + sondeo.** Con un worker secuencial no saca nada de la ruta
  caliente.
- **Montar el socket del host o Docker-in-Docker con privilegios.**
- **Contenedor genérico** en vez de la imagen del CI: más *drift*.
