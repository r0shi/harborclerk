import { useEffect, useRef } from 'react'
import { useAuth } from '../auth'

export interface FolderProgressEvent {
  folder_id: string
  total_files: number
  completed_files: number
  scan_status: 'scanning' | 'idle'
}

type Listener = (event: FolderProgressEvent) => void

/**
 * Shared SSE hook that connects to /api/watch/folders/stream,
 * parses per-folder progress deltas, and calls the listener on each
 * change. Reconnects automatically on disconnect. Mirrors useJobEvents.
 */
export function useFolderProgress(listener: Listener) {
  const { token } = useAuth()
  const listenerRef = useRef(listener)
  useEffect(() => {
    listenerRef.current = listener
  })

  useEffect(() => {
    if (!token) return

    const controller = new AbortController()
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined

    async function connect() {
      try {
        const res = await fetch('/api/watch/folders/stream', {
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
                const event = JSON.parse(line.slice(6)) as FolderProgressEvent
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
