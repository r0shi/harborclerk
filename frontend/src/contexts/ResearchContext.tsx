import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react'
import { currentToken, refreshToken } from '../api'
import { useAuth } from '../auth'

export interface ToolCallEntry {
  name: string
  arguments: Record<string, unknown>
  summary?: string
  rawResult?: string
  round?: number
}

export interface ResearchProgress {
  round: number
  maxRounds: number
  strategy: string
  reviewed?: number
  total?: number
  toolCalls: ToolCallEntry[]
  elapsedSeconds?: number
  timeLimitMinutes?: number
  notes?: string
}

interface ResearchState {
  isRunning: boolean
  isSynthesizing: boolean
  progress: ResearchProgress | null
  report: string
  error: string | null
  conversationId: string | null
  completedToolCalls: ToolCallEntry[]
  startResearch: (
    question: string,
    strategy?: string,
    timeLimitMinutes?: number,
    depth?: string,
    scope?: { folder_ids?: string[] },
  ) => Promise<boolean>
  resumeResearch: (convId: string) => Promise<void>
  cancelResearch: () => void
  reset: () => void
}

const ResearchContext = createContext<ResearchState | null>(null)

export function ResearchProvider({ children }: { children: ReactNode }) {
  const { token } = useAuth()
  const [isRunning, setIsRunning] = useState(false)
  const [isSynthesizing, setIsSynthesizing] = useState(false)
  const [progress, setProgress] = useState<ResearchProgress | null>(null)
  const [report, setReport] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [completedToolCalls, setCompletedToolCalls] = useState<ToolCallEntry[]>([])
  const abortRef = useRef<AbortController | null>(null)

  const fetchWithRefresh = useCallback(async (url: string, init: RequestInit): Promise<Response> => {
    const res = await fetch(url, init)
    if (res.status === 401) {
      const refreshed = await refreshToken()
      if (refreshed) {
        const headers = new Headers(init.headers)
        headers.set('Authorization', `Bearer ${currentToken()}`)
        return fetch(url, { ...init, headers })
      }
    }
    return res
  }, [])

  const processStream = useCallback(async (res: Response) => {
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }))
      throw new Error(err.detail || 'Research request failed')
    }

    if (!res.body) throw new Error('No response body')

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
        if (!line.startsWith('data: ')) continue
        try {
          const event = JSON.parse(line.slice(6))
          switch (event.type) {
            case 'progress':
              setProgress((prev) => ({
                round: event.round ?? event.step ?? 0,
                maxRounds: event.max_rounds ?? 0,
                strategy: event.strategy,
                reviewed: event.reviewed,
                total: event.total,
                toolCalls: prev?.toolCalls || [],
                elapsedSeconds: event.elapsed_seconds,
                timeLimitMinutes: event.time_limit_minutes,
                notes: prev?.notes,
              }))
              break

            case 'notes':
              setProgress((prev) => (prev ? { ...prev, notes: (prev.notes || '') + event.content + '\n' } : prev))
              break

            case 'tool_call':
              setProgress((prev) => {
                if (!prev) return prev
                return {
                  ...prev,
                  toolCalls: [...prev.toolCalls, { name: event.name, arguments: event.arguments, round: prev.round }],
                }
              })
              break

            case 'tool_result':
              setProgress((prev) => {
                if (!prev) return prev
                const toolCalls = [...prev.toolCalls]
                for (let i = toolCalls.length - 1; i >= 0; i--) {
                  if (toolCalls[i].name === event.name && !toolCalls[i].summary) {
                    toolCalls[i] = { ...toolCalls[i], summary: event.summary, rawResult: event.raw_result }
                    break
                  }
                }
                return { ...prev, toolCalls }
              })
              break

            case 'synthesis':
              setIsSynthesizing(true)
              break

            case 'token':
              setReport((prev) => prev + event.content)
              break

            case 'done':
              setConversationId(event.conversation_id || null)
              // Snapshot tool calls before clearing running state
              setProgress((prev) => {
                if (prev?.toolCalls.length) setCompletedToolCalls(prev.toolCalls)
                return prev
              })
              setIsRunning(false)
              setIsSynthesizing(false)
              break

            case 'error':
              setError(event.message || 'Research failed')
              setIsRunning(false)
              setIsSynthesizing(false)
              break
          }
        } catch {
          // ignore malformed events
        }
      }
    }
  }, [])

  const startResearch = useCallback(
    async (
      question: string,
      strategy?: string,
      timeLimitMinutes?: number,
      depth?: string,
      scope?: { folder_ids?: string[] },
    ): Promise<boolean> => {
      if (!token) return false

      setError(null)

      const controller = new AbortController()
      let accepted = false

      try {
        const body: Record<string, unknown> = { question }
        if (strategy) body.strategy = strategy
        if (timeLimitMinutes) body.time_limit_minutes = timeLimitMinutes
        if (depth) body.depth = depth
        if (scope && scope.folder_ids && scope.folder_ids.length > 0) body.scope = scope

        const res = await fetchWithRefresh('/api/research', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
          },
          body: JSON.stringify(body),
          signal: controller.signal,
        })

        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: res.statusText }))
          throw new Error(err.detail || `Research request failed (${res.status})`)
        }

        accepted = true
        const researchId = res.headers.get('X-Research-Id')

        abortRef.current?.abort()
        abortRef.current = controller
        setIsRunning(true)
        setIsSynthesizing(false)
        setProgress(null)
        setReport('')
        setCompletedToolCalls([])
        setConversationId(researchId || null)

        await processStream(res)
      } catch (e) {
        if (e instanceof DOMException && e.name === 'AbortError') return accepted
        setError(e instanceof Error ? e.message : 'An error occurred')
      } finally {
        setIsRunning(false)
        setIsSynthesizing(false)
        abortRef.current = null
      }
      return accepted
    },
    [token, fetchWithRefresh, processStream],
  )

  const resumeResearch = useCallback(
    async (convId: string) => {
      if (!token || isRunning) return

      setError(null)

      const controller = new AbortController()

      try {
        const res = await fetchWithRefresh(`/api/research/${convId}/resume`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
          },
          signal: controller.signal,
        })

        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: res.statusText }))
          throw new Error(err.detail || 'Resume failed')
        }

        // Server accepted — now safe to reset state
        abortRef.current?.abort()
        abortRef.current = controller
        setIsRunning(true)
        setIsSynthesizing(false)
        setProgress(null)
        setReport('')
        setConversationId(convId)

        await processStream(res)
      } catch (e) {
        if (e instanceof DOMException && e.name === 'AbortError') return
        setError(e instanceof Error ? e.message : 'An error occurred')
      } finally {
        setIsRunning(false)
        setIsSynthesizing(false)
        abortRef.current = null
      }
    },
    [token, isRunning, fetchWithRefresh, processStream],
  )

  const cancelResearch = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  const reset = useCallback(() => {
    setError(null)
    setReport('')
    setProgress(null)
    setConversationId(null)
    setCompletedToolCalls([])
  }, [])

  return (
    <ResearchContext.Provider
      value={{
        isRunning,
        isSynthesizing,
        progress,
        report,
        error,
        conversationId,
        completedToolCalls,
        startResearch,
        resumeResearch,
        cancelResearch,
        reset,
      }}
    >
      {children}
    </ResearchContext.Provider>
  )
}

export function useResearch(): ResearchState {
  const ctx = useContext(ResearchContext)
  if (!ctx) throw new Error('useResearch must be used within ResearchProvider')
  return ctx
}
