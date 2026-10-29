import { useCallback, useRef, useState } from 'react'
import {
  CDropdown,
  CDropdownItem,
  CDropdownMenu,
  CDropdownToggle,
  CFormInput,
  CInputGroup,
  CInputGroupText,
} from '@coreui/react'
import CIcon from '@coreui/icons-react'
import { cilReload } from '@coreui/icons'

import { useAutoRefresh, type RefreshPreset } from './AutoRefreshContext'

const PRESETS: { value: RefreshPreset; label: string }[] = [
  { value: 0, label: 'Desactivado' },
  { value: 5, label: '5 segundos' },
  { value: 10, label: '10 segundos' },
  { value: 30, label: '30 segundos' },
  { value: 60, label: '60 segundos' },
  { value: 'custom', label: 'Personalizado…' },
]

function formatLabel(preset: RefreshPreset, customValue: number): string {
  if (preset === 0) return 'Auto'
  if (preset === 'custom') return `${customValue}s`
  return `${preset}s`
}

export function RefreshSelector() {
  const { preset, customValue, setPreset } = useAutoRefresh()
  const [showCustomInput, setShowCustomInput] = useState(false)
  const [inputValue, setInputValue] = useState(String(customValue))
  const inputRef = useRef<HTMLInputElement>(null)

  const handleSelect = useCallback(
    (selected: RefreshPreset) => {
      if (selected === 'custom') {
        setShowCustomInput(true)
        setInputValue(String(customValue))
        setTimeout(() => inputRef.current?.focus(), 50)
      } else {
        setPreset(selected)
        setShowCustomInput(false)
      }
    },
    [customValue, setPreset],
  )

  const handleCustomSubmit = useCallback(() => {
    const value = parseInt(inputValue, 10)
    if (!isNaN(value) && value > 0) {
      setPreset('custom', value)
    }
    setShowCustomInput(false)
  }, [inputValue, setPreset])

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent) => {
      if (event.key === 'Enter') {
        handleCustomSubmit()
      } else if (event.key === 'Escape') {
        setShowCustomInput(false)
      }
    },
    [handleCustomSubmit],
  )

  return (
    <CDropdown variant="nav-item" alignment="end">
      <CDropdownToggle
        caret={false}
        className="d-flex align-items-center"
        title="Auto-actualización"
      >
        <CIcon icon={cilReload} size="lg" className={preset !== 0 ? 'text-success' : ''} />
        <span className="ms-1 small d-none d-md-inline">
          {formatLabel(preset, customValue)}
        </span>
      </CDropdownToggle>
      <CDropdownMenu className="refresh-selector-menu">
        <div className="px-3 py-2 small text-body-secondary border-bottom">
          Auto-actualización
        </div>
        {PRESETS.map(({ value, label }) => (
          <CDropdownItem
            key={String(value)}
            active={preset === value}
            onClick={() => handleSelect(value)}
          >
            {label}
          </CDropdownItem>
        ))}
        {showCustomInput && (
          <div className="px-3 py-2 border-top">
            <CInputGroup size="sm">
              <CFormInput
                ref={inputRef}
                type="number"
                min={1}
                max={3600}
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyDown={handleKeyDown}
                onBlur={handleCustomSubmit}
                placeholder="Segundos"
              />
              <CInputGroupText>s</CInputGroupText>
            </CInputGroup>
          </div>
        )}
      </CDropdownMenu>
    </CDropdown>
  )
}
