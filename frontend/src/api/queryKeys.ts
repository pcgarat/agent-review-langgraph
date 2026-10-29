/**
 * Claves de caché de TanStack Query, una por recurso del contrato.
 *
 * Existen en un solo sitio para que la invalidación tras un comando no dependa
 * de que cada pantalla recuerde la forma exacta de la clave: `invalidateQueries`
 * compara por prefijo, así que `state` invalida también lo que cuelgue de él.
 */
export const queryKeys = {
  session: ['session'] as const,
  state: ['state'] as const,
  history: (filters: { repo?: string; pr_id?: number } = {}) => ['history', filters] as const,
  bandeja: ['bandeja'] as const,
  config: ['config'] as const,
  review: (reviewKey: string) => ['review', reviewKey] as const,
} as const

/**
 * Claves que un comando deja obsoletas.
 *
 * Todo comando (lanzar, cancelar, decidir, publicar diales) mueve el estado
 * operativo, la bandeja y el histórico a la vez: invalidar solo «la pantalla
 * donde estoy» deja las otras dos mintiendo hasta el siguiente foco.
 */
export const AFTER_COMMAND = [queryKeys.state, queryKeys.bandeja] as const
