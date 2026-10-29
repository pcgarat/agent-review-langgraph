import { Navigate, Route, Routes, useParams } from 'react-router-dom'

import { AppLayout } from '@/components/AppLayout'
import { useSession } from '@/session/session'
import { BandejaPage } from '@/pages/BandejaPage'
import { ClaudePage } from '@/pages/ClaudePage'
import { ConfigPage } from '@/pages/ConfigPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { HistoryPage } from '@/pages/HistoryPage'
import { LoginPage } from '@/pages/LoginPage'
import { ReviewPage } from '@/pages/ReviewPage'
import { Splash } from '@/pages/Splash'

/** Guardia de sesión: sin usuario no hay nada que enseñar (029, P6). */
function Protected({ title, children }: { title: string; children: React.ReactNode }) {
  const { session } = useSession()
  if (session.status === 'loading') return <Splash />
  if (session.status === 'anonymous') return <Navigate to="/login" replace />
  return <AppLayout title={title}>{children}</AppLayout>
}

/**
 * La `review_key` es el resto de la ruta: lleva `/` y `:` dentro
 * (`cqm/emotivoo-v2:42:<sha>:<pipeline>`). Un segmento la cortaría.
 */
function ReviewRoute() {
  const params = useParams()
  const reviewKey = params['*'] ?? ''
  return (
    <Protected title={`Review ${reviewKey}`}>
      <ReviewPage reviewKey={reviewKey} />
    </Protected>
  )
}

export function AppRoutes() {
  const { session } = useSession()

  return (
    <Routes>
      <Route
        path="/login"
        element={
          session.status === 'authenticated' ? <Navigate to="/" replace /> : <LoginPage />
        }
      />
      <Route
        path="/"
        element={
          <Protected title="Cuadro de mando">
            <DashboardPage />
          </Protected>
        }
      />
      <Route
        path="/review/*"
        element={<ReviewRoute />}
      />
      <Route
        path="/bandeja"
        element={
          <Protected title="Bandeja de decisiones">
            <BandejaPage />
          </Protected>
        }
      />
      <Route
        path="/history"
        element={
          <Protected title="Histórico">
            <HistoryPage />
          </Protected>
        }
      />
      <Route
        path="/config"
        element={
          <Protected title="Configuración">
            <ConfigPage />
          </Protected>
        }
      />
      <Route
        path="/claude"
        element={
          <Protected title="Sesión de Claude">
            <ClaudePage />
          </Protected>
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
