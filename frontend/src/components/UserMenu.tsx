import { useNavigate } from 'react-router-dom'
import { CDropdown, CDropdownItem, CDropdownMenu, CDropdownToggle } from '@coreui/react'
import CIcon from '@coreui/icons-react'
import { cilAccountLogout, cilUser } from '@coreui/icons'

import { useSession } from '@/session/session'

export function UserMenu() {
  const { session, signOut } = useSession()
  const navigate = useNavigate()

  const handleSignOut = async () => {
    await signOut()
    navigate('/login', { replace: true })
  }

  return (
    <CDropdown variant="nav-item" placement="bottom-end">
      <CDropdownToggle caret={false} aria-label="Menú de sesión">
        <CIcon icon={cilUser} size="lg" />
      </CDropdownToggle>
      <CDropdownMenu>
        <CDropdownItem disabled>{session.user?.user ?? 'Sin sesión'}</CDropdownItem>
        <CDropdownItem as="button" type="button" onClick={() => void handleSignOut()}>
          <CIcon icon={cilAccountLogout} className="me-2" />
          Salir
        </CDropdownItem>
      </CDropdownMenu>
    </CDropdown>
  )
}
