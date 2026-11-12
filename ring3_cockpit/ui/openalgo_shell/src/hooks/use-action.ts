import { useCallback, useState } from 'react'

export interface ActionState<T> {
  pending: boolean
  error: string | null
  receipt: T | null
  run: (task: () => Promise<T>) => Promise<T | null>
  reset: () => void
}

/**
 * Drive one interactive operator action and keep its proof receipt.
 *
 * A failed action never clears the previous receipt: the operator keeps the last
 * good proof on screen while the gateway error is shown next to it.
 */
export function useAction<T>(): ActionState<T> {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [receipt, setReceipt] = useState<T | null>(null)

  const run = useCallback(async (task: () => Promise<T>) => {
    setPending(true)
    setError(null)
    try {
      const result = await task()
      setReceipt(result)
      return result
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'request failed')
      return null
    } finally {
      setPending(false)
    }
  }, [])

  const reset = useCallback(() => {
    setError(null)
    setReceipt(null)
  }, [])

  return { pending, error, receipt, run, reset }
}
