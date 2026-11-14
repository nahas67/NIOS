import { useCallback, useEffect, useState } from 'react'

/** The eleven production cockpit pages. */
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
  | 'agents'
  | 'backtest'

/** V1.1: physically separate workspaces + the three auth screens. */
export type WorkspaceKey = 'paper' | 'live' | 'legacy'
export type AuthScreen = 'setup' | 'login' | 'live-unlock' | null

/** Canonical URL path per page (legacy, workspace-less aliases). */
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
  agents: '/agents',
  backtest: '/backtest',
}

/** Workspace-qualified path for a page. */
export function workspacePath(page: PageKey, workspace: 'paper' | 'live'): string {
  return `/${workspace}/${page}`
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
  '/agents': 'agents',
  '/backtest': 'backtest',
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

/** Split `/paper/...`, `/live/...` and the auth screens out of a pathname. */
export function parseRoute(pathname: string): {
  workspace: WorkspaceKey
  page: PageKey
  authPage: AuthScreen
} {
  const path = normalize(pathname)
  if (path === '/setup' || path === '/login' || path === '/live-unlock') {
    return { workspace: 'legacy', page: 'dashboard', authPage: path.slice(1) as AuthScreen }
  }
  const match = /^\/(paper|live)(\/.*)?$/.exec(path)
  if (match) {
    const workspace = match[1] as 'paper' | 'live'
    const rest = match[2] && match[2].length > 1 ? match[2] : '/dashboard'
    return {
      workspace,
      page: PATH_TO_PAGE[rest] ?? 'dashboard',
      authPage: null,
    }
  }
  return { workspace: 'legacy', page: PATH_TO_PAGE[path] ?? 'dashboard', authPage: null }
}

/** Resolve a browser pathname to its page, defaulting to the dashboard. */
export function pageFromPath(pathname: string): PageKey {
  return parseRoute(pathname).page
}

export interface RouteState {
  path: string
  page: PageKey
  workspace: WorkspaceKey
  authPage: AuthScreen
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
    typeof window === 'undefined' ? '/' : normalize(window.location.pathname),
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

  const parsed = parseRoute(path)
  return {
    path,
    page: parsed.page,
    workspace: parsed.workspace,
    authPage: parsed.authPage,
    go,
  }
}
