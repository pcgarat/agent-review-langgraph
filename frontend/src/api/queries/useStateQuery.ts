import { useQuery } from '@tanstack/react-query'

import { api } from '@/api/client'
import { queryKeys } from '@/api/queryKeys'
import type { PanelState } from '@/api/types/state'

/**
 * Estado operativo (`GET /api/state`), compartido por el cuadro de mando y el
 * aviso de pendientes del sidebar.
 *
 * Una sola clave para las dos lecturas: si el sidebar preguntara por su cuenta,
 * cada navegación pediría el mismo documento dos veces y las cifras podrían
 * discrepar dentro de la misma pantalla.
 */
export function useStateQuery() {
  return useQuery({
    queryKey: queryKeys.state,
    queryFn: () => api.get<PanelState>('/api/state'),
  })
}
