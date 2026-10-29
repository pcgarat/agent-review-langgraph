import { CButton, CForm, CFormInput, CFormLabel } from '@coreui/react'
import type { FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { readParams, writeParams } from './params'

/**
 * Filtros del histórico en la cabecera (029, B3).
 *
 * Viven en la URL y no en estado local: el formulario HTML antiguo publicaba
 * `?repo=…&pr_id=…` y ese enlace se comparte. Se envían al pulsar «Filtrar», no en
 * cada tecla, para no pedir el histórico una vez por carácter.
 */
export function HistoryFilters() {
  const [search, setSearch] = useSearchParams()
  const params = readParams(search)

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    const repo = String(data.get('repo') ?? '').trim()
    const rawPr = String(data.get('pr_id') ?? '').trim()
    const prId = rawPr === '' ? undefined : Number(rawPr)
    // Cambiar el filtro vuelve a la primera página: la 7 de otro filtro no
    // significa nada aquí.
    setSearch(
      writeParams({
        repo,
        prId: prId !== undefined && Number.isInteger(prId) && prId > 0 ? prId : undefined,
        page: 1,
      }),
    )
  }

  return (
    <CForm
      className="history-filters"
      // La clave sale de la URL: al quitar el filtro, el campo vuelve a su valor
      // real aunque el usuario hubiera escrito otro sin enviarlo.
      key={`${params.repo}|${params.prId ?? ''}`}
      onSubmit={submit}
      role="search"
    >
      <div className="d-flex flex-wrap align-items-end gap-2">
        <div>
          <CFormLabel htmlFor="history-repo" className="eyebrow mb-1">
            Repo
          </CFormLabel>
          <CFormInput
            id="history-repo"
            name="repo"
            type="text"
            autoComplete="off"
            placeholder="org/repo"
            defaultValue={params.repo}
          />
        </div>
        <div>
          <CFormLabel htmlFor="history-pr" className="eyebrow mb-1">
            PR
          </CFormLabel>
          <CFormInput
            id="history-pr"
            name="pr_id"
            type="number"
            min={1}
            autoComplete="off"
            placeholder="123"
            defaultValue={params.prId ?? ''}
          />
        </div>
        <CButton type="submit" color="primary">
          Filtrar
        </CButton>
        <Link to="/history" className="btn btn-link btn-sm px-0">
          Quitar
        </Link>
      </div>
    </CForm>
  )
}
