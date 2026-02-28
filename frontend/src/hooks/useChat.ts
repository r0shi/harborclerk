import { useCallback, useRef, useState } from 'react'
import { useAuth } from '../auth'

export interface RagContextChunk {
  chunk_id: string
  doc_id: string
  doc_title: string
  page_start: number
  page_end: number
  score: number
  text: string
}

export interface ChatMessage {
  message_id?: string
  role: 'user' | 'assistant' | 'tool'
  content: string
  tool_calls?: ToolCallInfo[]
  rag_context?: RagContextChunk[]
  isStreaming?: boolean
}

export interface ToolCallInfo {
  name: string
  arguments: Record<string, unknown>
  result?: string
}

export function useChat() {
  const { token } = useAuth()
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [currentToolCall, setCurrentToolCall] = useState<ToolCallInfo | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const lastTitleRef = useRef<string | null>(null)

  const loadMessages = useCallback((msgs: ChatMessage[]) => {
    // Filter out tool messages for display — they're shown inline as tool cards
    setMessages(
      msgs.filter((m) => m.role !== 'tool'),
    )
  }, [])

  const sendMessage = useCallback(
    async (conversationId: string, content: string) => {
      if (!token || isStreaming) return

      // Add user message immediately
      lastTitleRef.current = null
      const userMsg: ChatMessage = { role: 'user', content }
      setMessages((prev) => [...prev, userMsg])
      setIsStreaming(true)
      setCurrentToolCall(null)

      const controller = new AbortController()
      abortRef.current = controller

      // Start with an empty assistant message for streaming
      const assistantMsg: ChatMessage = {
        role: 'assistant',
        content: '',
        tool_calls: [],
        isStreaming: true,
      }
      setMessages((prev) => [...prev, assistantMsg])

      try {
        const res = await fetch(
          `/api/chat/conversations/${conversationId}/messages`,
          {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              Authorization: `Bearer ${token}`,
            },
            body: JSON.stringify({ content }),
            signal: controller.signal,
          },
        )

        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: res.statusText }))
          throw new Error(err.detail || 'Failed to send message')
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
                case 'rag_context':
                  setMessages((prev) => {
                    const updated = [...prev]
                    const last = updated[updated.length - 1]
                    if (last && last.role === 'assistant') {
                      updated[updated.length - 1] = {
                        ...last,
                        rag_context: event.chunks,
                      }
                    }
                    return updated
                  })
                  break

                case 'token':
                  setMessages((prev) => {
                    const updated = [...prev]
                    const last = updated[updated.length - 1]
                    if (last && last.role === 'assistant') {
                      updated[updated.length - 1] = {
                        ...last,
                        content: last.content + event.content,
                      }
                    }
                    return updated
                  })
                  break

                case 'tool_call':
                  setCurrentToolCall({
                    name: event.name,
                    arguments: event.arguments,
                  })
                  break

                case 'tool_result':
                  setMessages((prev) => {
                    const updated = [...prev]
                    const last = updated[updated.length - 1]
                    if (last && last.role === 'assistant') {
                      updated[updated.length - 1] = {
                        ...last,
                        tool_calls: [
                          ...(last.tool_calls || []),
                          {
                            name: event.name,
                            arguments: {},
                            result: event.summary,
                          },
                        ],
                      }
                    }
                    return updated
                  })
                  setCurrentToolCall(null)
                  break

                case 'done':
                  if (event.title) {
                    lastTitleRef.current = event.title
                  }
                  setMessages((prev) => {
                    const updated = [...prev]
                    const last = updated[updated.length - 1]
                    if (last && last.role === 'assistant') {
                      updated[updated.length - 1] = {
                        ...last,
                        isStreaming: false,
                      }
                    }
                    return updated
                  })
                  break

                case 'error':
                  setMessages((prev) => {
                    const updated = [...prev]
                    const last = updated[updated.length - 1]
                    if (last && last.role === 'assistant') {
                      updated[updated.length - 1] = {
                        ...last,
                        content: `Error: ${event.message}`,
                        isStreaming: false,
                      }
                    }
                    return updated
                  })
                  break
              }
            } catch {
              // ignore malformed events
            }
          }
        }
      } catch (e) {
        if (e instanceof DOMException && e.name === 'AbortError') return
        setMessages((prev) => {
          const updated = [...prev]
          const last = updated[updated.length - 1]
          if (last && last.role === 'assistant') {
            updated[updated.length - 1] = {
              ...last,
              content: e instanceof Error ? `Error: ${e.message}` : 'An error occurred',
              isStreaming: false,
            }
          }
          return updated
        })
      } finally {
        setIsStreaming(false)
        setCurrentToolCall(null)
        abortRef.current = null
      }
    },
    [token, isStreaming],
  )

  const stopStreaming = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  return {
    messages,
    isStreaming,
    currentToolCall,
    sendMessage,
    stopStreaming,
    loadMessages,
    setMessages,
    lastTitle: lastTitleRef,
  }
}
