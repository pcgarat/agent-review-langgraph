import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
  type ReactElement,
  type ReactNode,
} from 'react'
import { CToast, CToastBody, CToastHeader, CToaster } from '@coreui/react'

export type ToastColor = 'success' | 'danger' | 'warning' | 'info'

export interface ToastMessage {
  title: string
  body?: string
  color?: ToastColor
}

interface ToastContextValue {
  /** Muestra un aviso efímero. Devuelve cuando el toast ya está en pantalla. */
  notify: (message: ToastMessage) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

/**
 * Avisos efímeros del panel.
 *
 * Vive en la raíz porque los usan varias pantallas a la vez (comandos del cuadro
 * de mando, respuestas de la bandeja, guardado de la configuración) y el
 * `CToaster` solo puede existir una vez en el árbol: dos toasters se pisan.
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toast, setToast] = useState<ReactElement<ComponentProps<typeof CToast>> | null>(null)
  const key = useRef(0)

  const notify = useCallback((message: ToastMessage) => {
    key.current += 1
    const color = message.color ?? 'success'
    setToast(
      <CToast key={key.current} autohide delay={4000} color={color} visible>
        <CToastHeader closeButton>
          <strong className="me-auto">{message.title}</strong>
        </CToastHeader>
        {message.body ? <CToastBody>{message.body}</CToastBody> : null}
      </CToast>,
    )
  }, [])

  const value = useMemo(() => ({ notify }), [notify])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <CToaster className="p-3" placement="bottom-end" push={toast ?? undefined} />
    </ToastContext.Provider>
  )
}

export function useToast(): ToastContextValue {
  const value = useContext(ToastContext)
  if (value === null) {
    throw new Error('useToast fuera de <ToastProvider>')
  }
  return value
}
