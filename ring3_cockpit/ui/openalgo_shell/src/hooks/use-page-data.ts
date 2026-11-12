import { useCallback, useEffect, useRef, useState } from 'react'

export interface PageDataState<T> {
  data: T | null
  error: string | null
  loading: boolean
  /** Re-fetch the page payload after an interactive operator action. */
  reload: () => void
}

/**
 * Load one cockpit page payload from the Ring 3 gateway.
 *
 * `load` must be a stable reference (a module-level API function), so the effect
 * runs exactly once per page mount and never loops on identity changes. Pages
 * that expose operator controls call `reload()` to re-hydrate their telemetry
 * after a mutation. A monotonic request id discards any in-flight response that
 * a newer reload has already superseded.
 */
export function usePageData<T>(load: () => Promise<T>): PageDataState<T> {
  const requestId = useRef(0)
  const [state, setState] = useState<Omit<PageDataState<T>, 'reload'>>({
    data: null,
    error: null,
    loading: true,
  })

  const fetchData = useCallback(() => {
    const id = requestId.current + 1
    requestId.current = id
    setState({ data: null, error: null, loading: true })
    load()
      .then((data) => {
        if (requestId.current === id) setState({ data, error: null, loading: false })
      })
      .catch((err: unknown) => {
        if (requestId.current === id) {
          setState({
            data: null,
            error: err instanceof Error ? err.message : 'request failed',
            loading: false,
          })
        }
      })
  }, [load])

  useEffect(() => {
    fetchData()
    return () => {
      requestId.current += 1
    }
  }, [fetchData])

  return { ...state, reload: fetchData }
}
