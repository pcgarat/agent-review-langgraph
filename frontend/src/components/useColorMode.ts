import { useCallback, useSyncExternalStore } from 'react'

export type ColorMode = 'light' | 'dark' | 'auto'

const STORAGE_KEY = 'panel-color-scheme'

function getSystemPreference(): 'light' | 'dark' {
  if (typeof window === 'undefined') return 'light'
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function getStoredMode(): ColorMode | null {
  if (typeof window === 'undefined') return null
  const stored = localStorage.getItem(STORAGE_KEY)
  if (stored === 'light' || stored === 'dark' || stored === 'auto') {
    return stored
  }
  return null
}

function resolveMode(mode: ColorMode): 'light' | 'dark' {
  return mode === 'auto' ? getSystemPreference() : mode
}

function applyTheme(mode: ColorMode) {
  const resolved = resolveMode(mode)
  document.documentElement.dataset.coreuiTheme = resolved
}

let listeners: (() => void)[] = []
let currentMode: ColorMode = getStoredMode() ?? 'light'

function subscribe(listener: () => void) {
  listeners = [...listeners, listener]
  return () => {
    listeners = listeners.filter((l) => l !== listener)
  }
}

function getSnapshot(): ColorMode {
  return currentMode
}

function setMode(mode: ColorMode) {
  currentMode = mode
  localStorage.setItem(STORAGE_KEY, mode)
  applyTheme(mode)
  listeners.forEach((l) => l())
}

if (typeof window !== 'undefined') {
  applyTheme(currentMode)

  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (currentMode === 'auto') {
      applyTheme('auto')
    }
  })
}

export function useColorMode() {
  const colorMode = useSyncExternalStore(subscribe, getSnapshot, () => 'light' as ColorMode)

  const setColorMode = useCallback((mode: ColorMode) => {
    setMode(mode)
  }, [])

  const resolvedMode = resolveMode(colorMode)

  return { colorMode, setColorMode, resolvedMode }
}
