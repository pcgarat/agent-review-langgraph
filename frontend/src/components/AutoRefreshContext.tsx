import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'

const STORAGE_KEY = 'panel.autoRefreshInterval'
const DEFAULT_INTERVAL = 0

export type RefreshPreset = 0 | 5 | 10 | 30 | 60 | 'custom'

export interface AutoRefreshContextValue {
  interval: number
  preset: RefreshPreset
  setPreset: (preset: RefreshPreset, customValue?: number) => void
  customValue: number
}

const AutoRefreshContext = createContext<AutoRefreshContextValue | null>(null)

function loadFromStorage(): { interval: number; preset: RefreshPreset; customValue: number } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw === null) return { interval: DEFAULT_INTERVAL, preset: 0, customValue: 15 }
    const parsed = JSON.parse(raw) as { interval?: number; preset?: RefreshPreset; customValue?: number }
    const interval = typeof parsed.interval === 'number' ? parsed.interval : DEFAULT_INTERVAL
    const customValue = typeof parsed.customValue === 'number' && parsed.customValue > 0 ? parsed.customValue : 15
    let preset: RefreshPreset = 0
    if (parsed.preset === 'custom') {
      preset = 'custom'
    } else if ([5, 10, 30, 60].includes(interval)) {
      preset = interval as RefreshPreset
    }
    return { interval, preset, customValue }
  } catch {
    return { interval: DEFAULT_INTERVAL, preset: 0, customValue: 15 }
  }
}

function saveToStorage(interval: number, preset: RefreshPreset, customValue: number): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ interval, preset, customValue }))
  } catch {
    // localStorage no disponible
  }
}

export function AutoRefreshProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState(() => loadFromStorage())

  const setPreset = useCallback((preset: RefreshPreset, customValue?: number) => {
    setState((current) => {
      let interval: number
      let newCustomValue = current.customValue

      if (preset === 'custom') {
        newCustomValue = customValue ?? current.customValue
        interval = newCustomValue
      } else {
        interval = preset
      }

      saveToStorage(interval, preset, newCustomValue)
      return { interval, preset, customValue: newCustomValue }
    })
  }, [])

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === STORAGE_KEY) {
        setState(loadFromStorage())
      }
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  return (
    <AutoRefreshContext.Provider
      value={{
        interval: state.interval,
        preset: state.preset,
        customValue: state.customValue,
        setPreset,
      }}
    >
      {children}
    </AutoRefreshContext.Provider>
  )
}

export function useAutoRefresh(): AutoRefreshContextValue {
  const context = useContext(AutoRefreshContext)
  if (context === null) {
    throw new Error('useAutoRefresh debe usarse dentro de AutoRefreshProvider')
  }
  return context
}

export function useRefetchInterval(): number | false {
  const { interval } = useAutoRefresh()
  return interval > 0 ? interval * 1000 : false
}
