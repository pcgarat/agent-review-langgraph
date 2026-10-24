import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'
import { AFTER_COMMAND } from '@/api/queryKeys'

/** Respuesta de `POST /api/claude/session` (`save_claude_session` en `web/main.py`). */
export interface ClaudeSessionSaved {
  /** Reviews que estaban paradas por falta de sesión y vuelven a la cola. */
  released: number
}

/**
 * Guardar el token del CLI (029, S8).
 *
 * El servidor no solo escribe el fichero `0600`: suelta lo que estaba parqueado
 * por falta de sesión (`ClaudeLogin.save`). Por eso hay que invalidar el estado y
 * la bandeja, o la cola seguiría pintando como paradas unas reviews que ya van a
 * arrancar. El token no se guarda en ningún sitio del cliente.
 */
export function useSaveClaudeToken() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (token: string) =>
      api.post<ClaudeSessionSaved>('/api/claude/session', { token }),
    onSuccess: () => {
      for (const key of AFTER_COMMAND) {
        void queryClient.invalidateQueries({ queryKey: key })
      }
    },
  })
}
