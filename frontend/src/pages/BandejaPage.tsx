import { CAlert, CButton, CCard, CCardBody } from '@coreui/react'

import { useRefetchInterval } from '@/components/AutoRefreshContext'
import { useSession } from '@/session/session'
import { explainError } from '@/pages/dashboard/errors'

import { PauseCard } from './bandeja/PauseCard'
import { useBandejaQuery } from './bandeja/useBandeja'
import './bandeja/bandeja.scss'

/**
 * Bandeja de decisiones (029, B1).
 *
 * Una tarjeta por pausa humana abierta: las mismas que el bot pintaría en Teams.
 * La primera respuesta gana, así que la pantalla no intenta cerrar tarjetas por su
 * cuenta: tras responder invalida la caché y vuelve a leer lo que el servidor diga.
 */
export function BandejaPage() {
  const refetchInterval = useRefetchInterval()
  const { data, isPending, isError, error, refetch } = useBandejaQuery({ refetchInterval })
  const { isOperador } = useSession()

  if (isPending) {
    return <p className="text-body-secondary small mb-0">Leyendo la bandeja…</p>
  }

  if (isError) {
    return (
      <CCard>
        <CCardBody>
          <p className="eyebrow mb-2">Sin datos</p>
          <CAlert color="danger" className="py-2 mb-3">
            {explainError(error, 'No se pudo leer la bandeja.')}
          </CAlert>
          <CButton type="button" color="primary" variant="outline" onClick={() => void refetch()}>
            Reintentar
          </CButton>
        </CCardBody>
      </CCard>
    )
  }

  return (
    <>
      <p className="eyebrow mb-3">
        {data.items.length === 0
          ? 'No hay decisiones pendientes'
          : `${data.items.length} ${data.items.length === 1 ? 'pausa esperando' : 'pausas esperando'}`}
      </p>

      <p className="small text-body-secondary">
        {data.teams_enabled
          ? 'Teams está configurado: la misma pregunta aparece allí y aquí. Vale la primera respuesta y la tarjeta desaparece de los dos sitios.'
          : 'Teams no está configurado: las preguntas se responden solo aquí.'}{' '}
        Siguen visibles hasta que alguien responde.
      </p>

      {!isOperador && data.items.length > 0 ? (
        <CAlert color="info" className="py-2 small">
          Tu rol es <strong>lector</strong>: puedes leer las preguntas, pero responder es del rol
          operador.
        </CAlert>
      ) : null}

      {data.items.length === 0 ? (
        <CCard>
          <CCardBody>
            <p className="text-body-secondary small mb-0">
              Ninguna review espera una decisión humana.
            </p>
          </CCardBody>
        </CCard>
      ) : (
        data.items.map((item) => (
          <PauseCard key={`${item.review_key}:${item.pause_id}`} item={item} isOperador={isOperador} />
        ))
      )}
    </>
  )
}
