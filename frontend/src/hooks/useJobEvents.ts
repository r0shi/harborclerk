import { useEffect, useRef } from 'react'
import { useAuth } from '../auth'

export interface JobEvent {
  version_id: string
  stage: string
  status: string
  progress?: number
  total?: number
  error?: string
  filename?: string
}

type Listener = (event: JobEvent) => void

/**
 * Shared SSE hook that connects to /api/jobs/stream,
 * parses flat JSON events from PostgreSQL LISTEN/NOTIFY, and
 * calls the provided listener on each event.
 * Reconnects automatically on disconnect.
 */
export function useJobEvents(listener: Listener) {
  const { token } = useAuth()
  const listenerRef = useRef(listener)
  listenerRef.current = listener

  useEffect(() => {
    if (!token) return

    const controller = new AbortController()
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined

    async function connect() {
      try {
        const res = await fetch('/api/jobs/stream', {
          headers: { Authorization: `Bearer ${token}` },
          signal: controller.signal,
        })
        if (!res.ok || !res.body) return

        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          for (const line of lines) {
            if (line.startsWith('data: ')) {
              try {
                const event = JSON.parse(line.slice(6)) as JobEvent
                listenerRef.current(event)
              } catch {
                // ignore malformed JSON
              }
            }
          }
        }
      } catch (e) {
        if (e instanceof DOMException && e.name === 'AbortError') return
      }
      // Reconnect after delay (unless aborted)
      if (!controller.signal.aborted) {
        reconnectTimer = setTimeout(connect, 5000)
      }
    }

    connect()

    return () => {
      controller.abort()
      if (reconnectTimer) clearTimeout(reconnectTimer)
    }
  }, [token])
}
