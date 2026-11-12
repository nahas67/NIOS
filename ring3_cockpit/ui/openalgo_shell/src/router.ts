import { useCallback, useEffect, useState } from 'react'

/** The nine production cockpit pages. */
export type PageKey =
  | 'dashboard'
  | 'orders'
  | 'analyzer'
  | 'quant'
  | 'brain'
  | 'forge'
  | 'ledger'
  | 'verification'
  | 'control'

/** Canonical URL path per page. */
export const PAGE_PATHS: Record<PageKey, string> = {
  dashboard: '/dashboard',
  orders: '/orders',
  analyzer: '/analyzer',
  quant: '/quant',
  brain: '/brain',
  forge: '/forge',
  ledger: '/ledger',
  verification: '/verification',
  control: '/control',
}

const PATH_TO_PAGE: Record<string, PageKey> = {
  '/': 'dashboard',
  '/dashboard': 'dashboard',
  '/orders': 'orders',
  '/positions': 'orders',
  '/analyzer': 'analyzer',
  '/quant': 'quant',
  '/brain': 'brain',
  '/xray': 'brain',
  '/forge': 'forge',
  '/ledger': 'ledger',
  '/verification': 'verification',
  '/control': 'control',
  '/mcp': 'control',
}

/** Legacy tab id each page maps onto, used for backward compatibility. */
export type LegacyTab = 'live' | 'xray' | 'control'

export function legacyTabFor(page: PageKey): LegacyTab | null {
  if (page === 'dashboard') return 'live'
  if (page === 'brain') return 'xray'
  if (page === 'control') return 'control'
  return null
}

export function pathForLegacyTab(tab: LegacyTab): string {
  if (tab === 'xray') return PAGE_PATHS.brain
  if (tab === 'control') return PAGE_PATHS.control
  return PAGE_PATHS.dashboard
}

function normalize(pathname: string): string {
  if (!pathname) return '/'
  const trimmed = pathname.replace(/\/+$/, '')
  return trimmed === '' ? '/' : trimmed
}

/** Resolve a browser pathname to its page, defaulting to the dashboard. */
export function pageFromPath(pathname: string): PageKey {
  return PATH_TO_PAGE[normalize(pathname)] ?? 'dashboard'
}

export interface RouteState {
  path: string
  page: PageKey
  go: (path: string) => void
}

/**
 * Minimal client-side router.
 *
 * The Ring 3 gateway serves `dist/index.html` for every non-API path, so a push
 * -state router is enough: direct navigation, refresh and back/forward all work
 * without any server-side route table.
 */
export function useRoute(): RouteState {
  const [path, setPath] = useState<string>(() =>
    typeof window === 'undefined' ? '/' : normalize(window.location.pathname)
  )

  useEffect(() => {
    const onPopState = () => setPath(normalize(window.location.pathname))
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const go = useCallback((next: string) => {
    const target = normalize(next)
    if (typeof window !== 'undefined' && window.location.pathname !== target) {
      window.history.pushState({}, '', target)
    }
    setPath(target)
  }, [])

  return { path, page: pageFromPath(path), go }
}
