from __future__ import annotations

import logging
import time

from reviews.settings import get_settings

logger = logging.getLogger(__name__)


def main() -> None:
    """Bucle del worker.

    Fase 0: comprueba que la configuracion carga y espera. La cola real (Redis o
    Postgres) y el consumo del grafo llegan con la Fase 1.
    """
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    logger.info("Worker de reviews arrancado (Fase 0)")
    logger.info("Politica: %s", settings.autonomy_config)
    logger.info("Perfil de repo: %s", settings.repo_profile_config)
    while True:
        time.sleep(30)


if __name__ == "__main__":
    main()
