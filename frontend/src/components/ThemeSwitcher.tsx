import { CDropdown, CDropdownItem, CDropdownMenu, CDropdownToggle } from '@coreui/react'
import CIcon from '@coreui/icons-react'
import { cilMoon, cilSun, cilLaptop } from '@coreui/icons'

import { useColorMode, type ColorMode } from './useColorMode'

const MODES: { value: ColorMode; label: string; icon: string[] }[] = [
  { value: 'light', label: 'Claro', icon: cilSun },
  { value: 'dark', label: 'Oscuro', icon: cilMoon },
  { value: 'auto', label: 'Sistema', icon: cilLaptop },
]

export function ThemeSwitcher() {
  const { colorMode, setColorMode, resolvedMode } = useColorMode()

  const currentIcon = resolvedMode === 'dark' ? cilMoon : cilSun

  return (
    <CDropdown variant="nav-item" placement="bottom-end">
      <CDropdownToggle caret={false} aria-label="Cambiar tema">
        <CIcon icon={currentIcon} size="lg" />
      </CDropdownToggle>
      <CDropdownMenu>
        {MODES.map(({ value, label, icon }) => (
          <CDropdownItem
            key={value}
            as="button"
            type="button"
            active={colorMode === value}
            onClick={() => setColorMode(value)}
          >
            <CIcon icon={icon} className="me-2" />
            {label}
          </CDropdownItem>
        ))}
      </CDropdownMenu>
    </CDropdown>
  )
}
