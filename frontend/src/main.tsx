import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { AppRoutes } from './router'
import { SessionContext } from './session/session'
import { useSessionState } from './session/useSessionState'
import './styles/panel.scss'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // El estado operativo cambia solo cuando el worker toca algo: refrescar al
      // volver a la ventana basta. Un `refetchInterval` global seria gasto de
      // Redis en pestanas olvidadas.
      refetchOnWindowFocus: true,
      retry: 1,
      staleTime: 5_000,
    },
  },
})

function Root() {
  const session = useSessionState()
  return (
    <SessionContext.Provider value={session}>
      <AppRoutes />
    </SessionContext.Provider>
  )
}

const container = document.getElementById('root')
if (container === null) {
  throw new Error('Falta #root en index.html')
}

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Root />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
