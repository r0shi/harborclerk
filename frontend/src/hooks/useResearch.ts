import { useCallback, useRef, useState } from 'react'
import { useAuth } from '../auth'

export interface ToolCallEntry {
  name: string
  arguments: Record<string, unknown>
  summary?: string
}

export interface ResearchProgress {
  round: number
  maxRounds: number
  strategy: string
  reviewed?: number
  total?: number
  toolCalls: ToolCallEntry[]
}

export function useResearch() {
  const { token } = useAuth()
  const [isRunning, setIsRunning] = useState(false)
  const [isSynthesizing, setIsSynthesizing] = useState(false)
  const [progress, setProgress] = useState<ResearchProgress | null>(null)
  const [report, setReport] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [conversationId, setConversationId] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

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
                round: event.round,
                maxRounds: event.max_rounds,
                strategy: event.strategy,
                reviewed: event.reviewed,
                total: event.total,
                toolCalls: prev?.toolCalls || [],
              }))
              break

            case 'tool_call':
              setProgress((prev) => {
                if (!prev) return prev
                return {
                  ...prev,
                  toolCalls: [...prev.toolCalls, { name: event.name, arguments: event.arguments }],
                }
              })
              break

            case 'tool_result':
              setProgress((prev) => {
                if (!prev) return prev
                // Find the last tool call with this name that has no summary
                const toolCalls = [...prev.toolCalls]
                for (let i = toolCalls.length - 1; i >= 0; i--) {
                  if (toolCalls[i].name === event.name && !toolCalls[i].summary) {
                    toolCalls[i] = { ...toolCalls[i], summary: event.summary }
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
    async (question: string, strategy?: string) => {
      if (!token || isRunning) return

      setIsRunning(true)
      setIsSynthesizing(false)
      setProgress(null)
      setReport('')
      setError(null)
      setConversationId(null)

      const controller = new AbortController()
      abortRef.current = controller

      try {
        const body: Record<string, unknown> = { question }
        if (strategy) body.strategy = strategy

        const res = await fetch('/api/research', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
          },
          body: JSON.stringify(body),
          signal: controller.signal,
        })

        // Extract conversation ID from header immediately (before streaming)
        const researchId = res.headers.get('X-Research-Id')
        if (researchId) {
          setConversationId(researchId)
        }

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
    [token, isRunning, processStream],
  )

  const resumeResearch = useCallback(
    async (convId: string) => {
      if (!token || isRunning) return

      setIsRunning(true)
      setIsSynthesizing(false)
      setProgress(null)
      setReport('')
      setError(null)
      setConversationId(convId)

      const controller = new AbortController()
      abortRef.current = controller

      try {
        const res = await fetch(`/api/research/${convId}/resume`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
          },
          signal: controller.signal,
        })

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
    [token, isRunning, processStream],
  )

  const cancelResearch = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  return {
    isRunning,
    isSynthesizing,
    progress,
    report,
    error,
    conversationId,
    startResearch,
    resumeResearch,
    cancelResearch,
  }
}
