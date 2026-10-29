import { useRef, useState } from 'react'
import { CButton, CFormInput, CFormSelect, CFormSwitch } from '@coreui/react'

import { dialKind, dialSpec } from './catalog'
import { isPristine, type DraftValue } from './draft'

interface DialControlProps {
  name: string
  initial: unknown
  value: DraftValue | undefined
  locked: boolean
  disabled: boolean
  onChange: (value: DraftValue) => void
}

/**
 * Un dial con su control, su ayuda y su Restaurar (029, C1/C2).
 *
 * El control sale del tipo del **valor inicial**, no del nombre: es la regla de
 * `_dial_control` en `ui.py`. Un `Literal` no se distingue de un texto por su
 * valor, así que su lista de opciones viene del catálogo.
 */
export function DialControl({
  name,
  initial,
  value,
  locked,
  disabled,
  onChange,
}: DialControlProps) {
  const kind = dialKind(name, initial)
  const spec = dialSpec(name)
  const bounds = spec?.bounds
  const canDisable = spec?.canDisable === true && kind === 'number'
  // Mientras se escribe, un «0» no apaga el tope: desmontaría el campo con el
  // foco dentro. Al salir del campo, 0 sí significa apagado.
  const [editing, setEditing] = useState(false)
  const off = canDisable && value === '0' && !editing
  const remembered = useRef(positiveSeed(off ? initial : value))
  const pristine = isPristine(kind, value, initial)
  const controlId = `dial-${name}`
  const helpId = `dial-${name}-help`

  return (
    <div className="config-dial" data-testid={`dial-${name}`}>
      <div className="config-dial-head">
        <label className="config-dial-name" htmlFor={controlId}>
          {name}
        </label>
        <code className="config-dial-value num small text-body-secondary">
          inicial {formatInitial(initial)}
        </code>
        <CButton
          type="button"
          size="sm"
          color="secondary"
          variant="ghost"
          className="ms-auto"
          // Restaurar sin cambio no hace nada: un botón activo ahí es ruido.
          disabled={pristine || locked || disabled}
          onClick={() => onChange(kind === 'bool' ? (initial === true) : String(initial ?? ''))}
        >
          Restaurar
        </CButton>
      </div>

      <div className="config-dial-control">
        {kind === 'bool' ? (
          <CFormSwitch
            id={controlId}
            checked={value === true}
            disabled={locked || disabled}
            aria-describedby={helpId}
            onChange={(event) => onChange(event.target.checked)}
          />
        ) : kind === 'enum' ? (
          <CFormSelect
            id={controlId}
            value={typeof value === 'string' ? value : ''}
            disabled={locked || disabled}
            aria-describedby={helpId}
            options={enumOptions(spec?.options ?? [], initial)}
            onChange={(event) => onChange(event.target.value)}
          />
        ) : (
          <div className="d-flex flex-column gap-2">
            {canDisable ? (
              <CFormSwitch
                id={`${controlId}-enabled`}
                label="Limitar turnos"
                checked={!off}
                disabled={locked || disabled}
                aria-describedby={helpId}
                onChange={(event) => {
                  if (event.target.checked) {
                    onChange(remembered.current)
                    return
                  }
                  const current = Number(value)
                  if (Number.isFinite(current) && current >= 1) {
                    remembered.current = String(current)
                  }
                  onChange('0')
                }}
              />
            ) : null}
            {off ? null : (
              <CFormInput
                id={controlId}
                type={kind === 'number' ? 'number' : 'text'}
                inputMode={kind === 'number' ? 'decimal' : undefined}
                min={canDisable ? 1 : bounds?.min}
                max={bounds?.max}
                step={bounds?.step}
                autoComplete="off"
                value={typeof value === 'string' ? value : ''}
                disabled={locked || disabled}
                aria-describedby={helpId}
                onFocus={() => setEditing(true)}
                onBlur={() => setEditing(false)}
                onChange={(event) => onChange(event.target.value)}
              />
            )}
          </div>
        )}
      </div>

      <p id={helpId} className="config-dial-help small text-body-secondary mb-0">
        {spec?.help ?? 'Sin ayuda: esta versión del panel no conoce este dial.'}
        {locked ? ' Lo fija el despliegue: PANEL_CONFIG_LOCK lo bloquea.' : null}
      </p>
    </div>
  )
}

/** El inicial tal como se lee en el paso de la página HTML: sin comillas raras. */
function positiveSeed(initial: unknown): string {
  const parsed = Number(initial)
  return Number.isFinite(parsed) && parsed >= 1 ? String(parsed) : '200'
}

function formatInitial(initial: unknown): string {
  if (typeof initial === 'boolean') return initial ? 'sí' : 'no'
  if (initial === '') return '(vacío)'
  return String(initial ?? '—')
}

/**
 * Opciones del select, con el inicial delante si no está entre las declaradas.
 *
 * `CFormSelect` ignora `children` cuando recibe `options`, así que la lista se
 * compone aquí. El entorno puede haber arrancado con un valor fuera del `Literal`
 * (más viejo o más nuevo que este panel): sin su opción, el select enseñaría la
 * primera y parecería un cambio que nadie hizo.
 */
function enumOptions(
  allowed: readonly string[],
  initial: unknown,
): { value: string; label: string }[] {
  const options = allowed.map((value) => ({ value, label: value }))
  const painted = String(initial ?? '')
  if (allowed.includes(painted)) return options
  return [{ value: painted, label: `${painted} (inicial)` }, ...options]
}
