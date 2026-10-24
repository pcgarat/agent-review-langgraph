import { CAlert, CBadge, CCard, CCardBody, CCardHeader } from '@coreui/react'

import type { ConfigDocument } from '@/api/types/config'

import { formatDateTime, plural } from '@/pages/dashboard/format'

import { groupPins } from './pins'

interface ConfigStatusProps {
  document: ConfigDocument
}

/**
 * Cabecera del documento publicable: qué revisión está vigente, quién la firmó y
 * qué revisiones tienen fijadas las reviews en curso (029, C4).
 *
 * `pins` ausente **no** es una lista vacía. El servidor omite el campo cuando el
 * checkpointer no respondió, y decir «ninguna review ha fijado revisión» sería
 * afirmar algo que no se pudo comprobar.
 */
export function ConfigStatus({ document }: ConfigStatusProps) {
  const pins = document.pins === undefined ? null : groupPins(document.pins)

  return (
    <CCard className="mb-4">
      <CCardHeader className="d-flex flex-wrap align-items-baseline gap-2">
        <span className="eyebrow mb-0">Revisión publicada</span>
        <strong className="num fs-5">{document.revision}</strong>
        <span className="small text-body-secondary">
          {document.revision === 0
            ? 'Ningún dial se ha publicado todavía: mandan los valores del despliegue.'
            : `Firmada por ${document.actor} · ${formatDateTime(document.updated_at)}`}
        </span>
      </CCardHeader>
      <CCardBody className="py-3">
        <p className="eyebrow mb-2">Revisiones fijadas por reviews en curso</p>
        {pins === null ? (
          <CAlert color="warning" className="py-2 mb-0 small">
            No se pudo leer la foto de las reviews en curso: el checkpointer no respondió. No
            significa que ninguna haya fijado revisión, solo que ahora no se sabe.
          </CAlert>
        ) : pins.length === 0 ? (
          <p className="small text-body-secondary mb-0">
            Ninguna review en curso ha fijado una revisión todavía.
          </p>
        ) : (
          <ul className="list-unstyled mb-0 config-pins">
            {pins.map((group) => (
              <li
                key={group.revision}
                className="d-flex flex-wrap align-items-baseline gap-2 mb-1"
              >
                <CBadge color="info" className="num">
                  revisión {group.revision}
                </CBadge>
                <span className="small">
                  {group.reviewKeys.length === 1 ? (
                    <>
                      en uso por <code>{group.reviewKeys[0]}</code>
                    </>
                  ) : (
                    <>en uso por {plural(group.reviewKeys.length, 'review', 'reviews')}</>
                  )}
                </span>
                {group.reviewKeys.length > 1 ? (
                  <span className="small text-body-secondary text-break">
                    {group.reviewKeys.join(' · ')}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </CCardBody>
    </CCard>
  )
}
