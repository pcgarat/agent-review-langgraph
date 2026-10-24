import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api, reviewPath } from '@/api/client'
import { AFTER_COMMAND } from '@/api/queryKeys'
import { useToast, type ToastColor } from '@/components/ToastProvider'

import { explainError } from './errors'

export type PanelCommand = 'cancel' | 'retry' | 'forget'

/** Respuesta de `/api/reviews/{review_key}/{comando}` (`_run_command` en `main.py`). */
export interface CommandResult {
  review_key: string
  effect: string
  actor: string
}

const COMMAND_LABEL: Record<PanelCommand, string> = {
  cancel: 'cancelar',
  retry: 'reintentar',
  forget: 'olvidar',
}

/**
 * Qué decir al terminar, según el `effect` que devolvió el servidor.
 *
 * Cancelar tiene dos desenlaces que no se pueden confundir: una review que ya
 * publicó solo puede cancelar lo pendiente. Es el servidor quien lo sabe, así que
 * el texto sale de su respuesta y no se adivina en el cliente.
 */
const EFFECT_TOAST: Record<string, { title: string; body: string; color: ToastColor }> = {
  cancelled: {
    title: 'Review cancelada',
    body: 'Lo que estaba en curso se detiene antes de publicar.',
    color: 'success',
  },
  cancelled_pending: {
    title: 'Cancelado lo pendiente',
    body: 'La review ya había publicado: los comentarios del PR se quedan.',
    color: 'warning',
  },
  retried: {
    title: 'Review reencolada',
    body: 'Vuelve a la cola con los intentos a cero.',
    color: 'success',
  },
  forgotten: {
    title: 'Borrado encolado',
    body: 'El worker retirará el hilo y el artefacto. Los comentarios publicados se quedan.',
    color: 'success',
  },
}

export interface CommandRequest {
  reviewKey: string
  command: PanelCommand
}

/**
 * Comandos del cuadro de mando (029, P11).
 *
 * Todo pasa por aquí para que ningún botón olvide lo que hay que hacer después:
 * invalidar `AFTER_COMMAND` y avisar. La invalidación es la única forma de que las
 * cifras y la bandeja digan la verdad tras mover una review; `onSettled` la hace
 * también cuando el comando falla, porque el fallo no garantiza que nada cambió.
 */
export function usePanelCommand() {
  const queryClient = useQueryClient()
  const { notify } = useToast()

  return useMutation({
    mutationFn: ({ reviewKey, command }: CommandRequest) =>
      api.post<CommandResult>(`${reviewPath(reviewKey)}/${command}`),
    onSuccess: (result, { command }) => {
      const effect = EFFECT_TOAST[result.effect]
      notify(
        effect ?? {
          title: `Petición de ${COMMAND_LABEL[command]} aceptada`,
          body: `El servidor respondió «${result.effect}».`,
          color: 'info',
        },
      )
    },
    onError: (error, { command }) => {
      notify({
        title: `No se pudo ${COMMAND_LABEL[command]}`,
        body: explainError(error),
        color: 'danger',
      })
    },
    onSettled: () => {
      for (const key of AFTER_COMMAND) {
        void queryClient.invalidateQueries({ queryKey: key })
      }
    },
  })
}
