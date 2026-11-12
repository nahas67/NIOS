import { useEffect, useState } from 'react'

export interface PageDataState<T> {
  data: T | null
  error: string | null
  loading: boolean
}

/**
 * Load one read-only page payload from the Ring 3 gateway.
 *
 * `load` must be a stable reference (a module-level API function), so the effect
 * runs exactly once per page mount and never loops on identity changes.
 */
export function usePageData<T>(load: () => Promise<T>): PageDataState<T> {
  const [state, setState] = useState<PageDataState<T>>({
    data: null,
    error: null,
    loading: true,
  })

  useEffect(() => {
    let alive = true
    setState({ data: null, error: null, loading: true })
    load()
      .then((data) => {
        if (alive) setState({ data, error: null, loading: false })
      })
      .catch((err: unknown) => {
        if (alive) {
          setState({
            data: null,
            error: err instanceof Error ? err.message : 'request failed',
            loading: false,
          })
        }
      })
    return () => {
      alive = false
    }
  }, [load])

  return state
}
