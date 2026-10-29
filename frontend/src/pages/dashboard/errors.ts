import { ApiError } from '@/api/client'

/**
 * Motivo de un fallo de la API, para enseñarlo tal cual.
 *
 * El `detail` de FastAPI ya explica el caso (rol, conflicto, Redis caído), así que
 * no se sustituye por un texto genérico que borre la causa.
 */
export function explainError(error: unknown, fallback = 'No se pudo hablar con el panel.'): string {
  if (error instanceof ApiError) return error.detailText
  if (error instanceof Error && error.message.trim() !== '') return error.message
  return fallback
}
