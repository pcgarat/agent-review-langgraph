import { CAlert, CButton, CCard, CCardBody, CCardHeader, CCol, CRow } from '@coreui/react'
import { useCallback, useMemo, useState } from 'react'

import { ApiError } from '@/api/client'
import { useRefetchInterval } from '@/components/AutoRefreshContext'
import { useSession } from '@/session/session'

import { explainError } from '@/pages/dashboard/errors'

import { ConfigStatus } from './config/ConfigStatus'
import { DialControl } from './config/DialControl'
import { groupDials } from './config/catalog'
import {
  changedNames,
  describeSaveFailure,
  savePayload,
  seedForm,
  type Draft,
  type SaveFailure,
} from './config/draft'
import { useConfigQuery, useSaveConfig } from './config/useConfig'
import './config/config.scss'

/** El formulario en edición y la revisión a la que pertenece. */
interface EditedForm {
  revision: number
  values: Draft
  /** `false` cuando lo que hay es lo que el servidor acaba de publicar. */
  dirty: boolean
}

export function ConfigPage() {
  const refetchInterval = useRefetchInterval()
  const { data, isPending, isError, error, refetch, isFetching } = useConfigQuery({ refetchInterval })
  const { isOperador } = useSession()
  const save = useSaveConfig()
  const [edited, setEdited] = useState<EditedForm | null>(null)
  const [failure, setFailure] = useState<SaveFailure | null>(null)

  const document = data

  // El borrador manda mientras no haya uno más nuevo que el documento leído. Se
  // conserva el de la revisión recién guardada hasta que la recarga la confirme,
  // para no enseñar durante un instante los valores que se acaban de sustituir.
  const values = useMemo(() => {
    if (document === undefined) return {}
    if (edited !== null && edited.revision >= document.revision) return edited.values
    return seedForm(document).values
  }, [document, edited])

  const changes = useMemo(() => {
    if (document === undefined) return []
    // Lo que el servidor acaba de aceptar no está pendiente aunque sus valores
    // sigan apartándose del inicial: ya son la revisión publicada.
    if (edited !== null && !edited.dirty) return []
    return changedNames({ revision: document.revision, values }, document)
  }, [document, edited, values])

  const update = useCallback(
    (name: string, value: string | boolean) => {
      if (document === undefined) return
      setFailure(null)
      setEdited((current) => {
        const base =
          current !== null && current.revision >= document.revision
            ? current.values
            : seedForm(document).values
        return { revision: document.revision, values: { ...base, [name]: value }, dirty: true }
      })
    },
    [document],
  )

  if (isPending) {
    return <p className="text-body-secondary small mb-0">Leyendo la configuración publicada…</p>
  }

  if (isError || document === undefined) {
    return (
      <CCard>
        <CCardBody>
          <p className="eyebrow mb-2">Sin datos</p>
          <CAlert color="danger" className="py-2 mb-3">
            {explainError(error, 'No se pudo leer la configuración.')}
          </CAlert>
          <CButton type="button" color="primary" variant="outline" onClick={() => void refetch()}>
            Reintentar
          </CButton>
        </CCardBody>
      </CCard>
    )
  }

  const groups = groupDials(document.initial)
  const locked = document.locked

  const submit = () => {
    const payload = savePayload({ revision: document.revision, values }, document)
    save.mutate(payload, {
      onSuccess: (result) => {
        // Lo guardado ya es la revisión nueva: se queda en pantalla hasta que la
        // recarga de la caché confirme lo mismo, sin un parpadeo a los valores viejos.
        setEdited({ revision: result.revision, values, dirty: false })
        setFailure(null)
      },
      onError: (error) => {
        const described = describeSaveFailure(error)
        setFailure(described)
        // Un 409 significa que la pantalla se pintó sobre otra cosa: el borrador
        // se descarta para no volver a mandar el mismo formulario obsoleto.
        if (error instanceof ApiError && error.status === 409) setEdited(null)
      },
    })
  }

  return (
    <>
      <p className="eyebrow mb-3">
        Cada control vuelve a su valor inicial con Restaurar. Guardar publica la revisión.
      </p>

      <ConfigStatus document={document} />

      {locked.length > 0 ? (
        <CAlert color="info" className="py-2 small">
          <strong>{locked.length === 1 ? 'Un dial está bloqueado' : `${locked.length} diales están bloqueados`}</strong>{' '}
          por <code>PANEL_CONFIG_LOCK</code>: el despliegue fija su valor y aquí solo se
          puede leer. {locked.join(', ')}.
        </CAlert>
      ) : null}

      {!isOperador ? (
        <CAlert color="info" className="py-2 small">
          Tu rol es <strong>lector</strong>: puedes leer los diales, pero guardarlos es del rol
          operador.
        </CAlert>
      ) : null}

      {failure !== null ? (
        <CAlert color={failure.reload ? 'warning' : 'danger'} className="py-2 small">
          <strong className="d-block">{failure.title}</strong>
          {failure.body}
          {failure.reload ? (
            <CButton
              type="button"
              size="sm"
              color="secondary"
              variant="outline"
              className="ms-2"
              disabled={isFetching}
              onClick={() => void refetch()}
            >
              Recargar el documento
            </CButton>
          ) : null}
        </CAlert>
      ) : null}

      {groups.map((group) => (
        <CCard key={group.section} className="mb-4">
          <CCardHeader>
            <h2 className="h6 mb-0">{group.section}</h2>
          </CCardHeader>
          <CCardBody>
            <CRow className="g-4">
              {group.names.map((name) => (
                <CCol key={name} xs={12} xl={6}>
                  <DialControl
                    name={name}
                    initial={document.initial[name]}
                    value={values[name]}
                    locked={locked.includes(name)}
                    // Un lector no puede guardar (el `403` es del servidor), así que
                    // dejarle editar solo produce un borrador sin salida.
                    disabled={!isOperador || save.isPending}
                    onChange={(value) => update(name, value)}
                  />
                </CCol>
              ))}
            </CRow>
          </CCardBody>
        </CCard>
      ))}

      {isOperador && changes.length > 0 ? (
        <div className="config-save-bar">
          <span className="small text-body-secondary">
            {changes.length === 1 ? '1 dial cambiado' : `${changes.length} diales cambiados`} sin
            publicar. Revisión {document.revision}.
          </span>
          <CButton
            type="button"
            color="secondary"
            variant="outline"
            size="sm"
            disabled={save.isPending}
            onClick={() => {
              setEdited(null)
              setFailure(null)
            }}
          >
            Descartar cambios
          </CButton>
          <CButton type="button" color="primary" size="sm" disabled={save.isPending} onClick={submit}>
            {save.isPending ? 'Guardando…' : 'Guardar'}
          </CButton>
        </div>
      ) : isOperador ? (
        <p className="small text-body-secondary mb-0">
          No hay cambios sin publicar. Guardar solo escribe los diales que se apartan del valor
          inicial.
          {Object.keys(document.values).length > 0
            ? ` Ahora mismo hay ${Object.keys(document.values).length} publicados por encima del inicial.`
            : ''}
        </p>
      ) : null}
    </>
  )
}
