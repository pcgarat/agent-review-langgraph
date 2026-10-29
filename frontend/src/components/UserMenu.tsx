import { useNavigate } from 'react-router-dom'
import {
  CAvatar,
  CBadge,
  CDropdown,
  CDropdownDivider,
  CDropdownHeader,
  CDropdownItem,
  CDropdownMenu,
  CDropdownToggle,
} from '@coreui/react'
import CIcon from '@coreui/icons-react'
import { cilAccountLogout, cilUser } from '@coreui/icons'

import { useSession } from '@/session/session'

function initials(user: string): string {
  const letters = user
    .split(/[\s._@-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? '')
    .join('')
  return letters || '?'
}

/** Avatar y desplegable de la cuenta, como `AppHeaderDropdown` de la plantilla. */
export function UserMenu() {
  const { session, signOut } = useSession()
  const navigate = useNavigate()
  const user = session.user?.user ?? ''

  const handleSignOut = async () => {
    await signOut()
    navigate('/login', { replace: true })
  }

  return (
    <CDropdown variant="nav-item" placement="bottom-end">
      <CDropdownToggle className="py-0 pe-0" caret={false} aria-label="Menú de sesión">
        <CAvatar color="primary" textColor="white" size="md">
          {user ? initials(user) : <CIcon icon={cilUser} />}
        </CAvatar>
      </CDropdownToggle>
      <CDropdownMenu className="pt-0">
        <CDropdownHeader className="bg-body-secondary fw-semibold mb-2">Cuenta</CDropdownHeader>
        <CDropdownItem disabled className="d-flex align-items-center">
          <CIcon icon={cilUser} className="me-2" />
          {user || 'Sin sesión'}
          {session.user ? (
            <CBadge color={session.user.role === 'operador' ? 'primary' : 'secondary'} className="ms-2">
              {session.user.role}
            </CBadge>
          ) : null}
        </CDropdownItem>
        <CDropdownDivider />
        <CDropdownItem as="button" type="button" onClick={() => void handleSignOut()}>
          <CIcon icon={cilAccountLogout} className="me-2" />
          Salir
        </CDropdownItem>
      </CDropdownMenu>
    </CDropdown>
  )
}
