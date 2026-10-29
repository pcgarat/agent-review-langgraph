import { CDropdown, CDropdownItem, CDropdownMenu, CDropdownToggle } from '@coreui/react'
import CIcon from '@coreui/icons-react'
import { cilContrast, cilMoon, cilSun } from '@coreui/icons'

import { useColorMode, type ColorMode } from './useColorMode'

const MODES: { value: ColorMode; label: string; icon: string[] }[] = [
  { value: 'light', label: 'Claro', icon: cilSun },
  { value: 'dark', label: 'Oscuro', icon: cilMoon },
  { value: 'auto', label: 'Automático', icon: cilContrast },
]

export function ThemeSwitcher() {
  const { colorMode, setColorMode } = useColorMode()

  const current = MODES.find((mode) => mode.value === colorMode) ?? MODES[0]!

  return (
    <CDropdown variant="nav-item" placement="bottom-end">
      <CDropdownToggle caret={false} aria-label="Cambiar tema">
        <CIcon icon={current.icon} size="lg" />
      </CDropdownToggle>
      <CDropdownMenu>
        {MODES.map(({ value, label, icon }) => (
          <CDropdownItem
            key={value}
            as="button"
            type="button"
            className="d-flex align-items-center"
            active={colorMode === value}
            onClick={() => setColorMode(value)}
          >
            <CIcon icon={icon} size="lg" className="me-2" />
            {label}
          </CDropdownItem>
        ))}
      </CDropdownMenu>
    </CDropdown>
  )
}
