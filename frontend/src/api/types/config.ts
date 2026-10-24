/** Configuración publicada de los diales (`GET /api/config`, iniciativa 027). */

/** Revisión de diales fijada por una review en curso (spec 029, P9). */
export interface DialPin {
  review_key: string
  revision: number
}

export interface ConfigDocument {
  revision: number
  /** Solo las claves que se apartan del valor inicial. */
  values: Record<string, unknown>
  initial: Record<string, unknown>
  /** Nombres de dial bloqueados por `PANEL_CONFIG_LOCK`. */
  locked: string[]
  actor: string
  updated_at: string | null
  pins?: DialPin[]
}
