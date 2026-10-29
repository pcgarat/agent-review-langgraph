import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'
import { AFTER_COMMAND, queryKeys } from '@/api/queryKeys'
import type { ConfigDocument } from '@/api/types/config'
import { useToast } from '@/components/ToastProvider'

import { describeSaveFailure, type SavePayload } from './draft'

/**
 * Documento vigente de los diales (`GET /api/config`, 029 P9).
 *
 * Trae `pins` solo si el checkpointer respondió: la ausencia es un dato, no un
 * valor por defecto, y quien lo pinta no puede tratarla como lista vacía.
 */
export function useConfigQuery() {
  return useQuery({
    queryKey: queryKeys.config,
    queryFn: () => api.get<ConfigDocument>('/api/config'),
  })
}

/** Respuesta de `POST /config`: la revisión nueva, nada más. */
export interface SaveResult {
  revision: number
}

/**
 * Guardar los diales (029, C3).
 *
 * Invalida `AFTER_COMMAND` y la propia configuración: publicar diales mueve el
 * estado operativo (una review nueva usa otros topes) y deja obsoleto el
 * documento que se acaba de leer. El `409` no es un fallo de red y se explica en
 * pantalla además de en el toast, porque obliga a recargar y el aviso efímero se
 * va antes de que se lea.
 */
export function useSaveConfig() {
  const queryClient = useQueryClient()
  const { notify } = useToast()

  return useMutation({
    mutationFn: (payload: SavePayload) => api.post<SaveResult>('/config', payload),
    onSuccess: (result) => {
      notify({
        title: 'Configuración publicada',
        body: `La revisión ${result.revision} ya es la vigente. Las reviews nuevas la usarán; las que están en curso conservan su foto.`,
      })
    },
    onError: (error) => {
      const failure = describeSaveFailure(error)
      notify({ title: failure.title, body: failure.body, color: 'danger' })
    },
    onSettled: () => {
      for (const key of AFTER_COMMAND) {
        void queryClient.invalidateQueries({ queryKey: key })
      }
      void queryClient.invalidateQueries({ queryKey: queryKeys.config })
    },
  })
}
