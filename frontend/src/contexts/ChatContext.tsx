import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react'
import { useAuth } from '../auth'

export interface RagContextChunk {
  chunk_id: string
  doc_id: string
  doc_title: string
  page_start: number | null
  page_end: number | null
  score: number
  text: string
}

export interface ChatMessage {
  message_id?: string
  role: 'user' | 'assistant' | 'tool'
  content: string
  tool_calls?: ToolCallInfo[]
  rag_context?: RagContextChunk[]
  errorDetail?: string
  isStreaming?: boolean
  model_id?: string
  context_pct?: number
}

export interface ToolCallInfo {
  name: string
  arguments: Record<string, unknown>
  result?: string
}

interface ChatState {
  /** The conversation ID that the current messages/stream belong to. */
  activeConversationId: string | null
  messages: ChatMessage[]
  isStreaming: boolean
  currentToolCall: ToolCallInfo | null
  latestTitle: string | null
  sendMessage: (conversationId: string, content: string) => Promise<void>
  stopStreaming: () => void
  loadMessages: (conversationId: string, msgs: ChatMessage[]) => void
  setMessages: React.Dispatch<React.SetStateAction<ChatMessage[]>>
  lastTitle: React.RefObject<string | null>
}

const ChatContext = createContext<ChatState | null>(null)

export function ChatProvider({ children }: { children: ReactNode }) {
  const { token } = useAuth()
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [currentToolCall, setCurrentToolCall] = useState<ToolCallInfo | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const lastTitleRef = useRef<string | null>(null)
  const [latestTitle, setLatestTitle] = useState<string | null>(null)

  // Ref mirror of activeConversationId — readable inside setMessages functional updates.
  const activeConvRef = useRef<string | null>(null)

  const loadMessages = useCallback((conversationId: string, msgs: ChatMessage[]) => {
    activeConvRef.current = conversationId
    setActiveConversationId(conversationId)
    setMessages(msgs.filter((m) => m.role !== 'tool'))
    lastTitleRef.current = null
    setLatestTitle(null)
  }, [])

  const sendMessage = useCallback(
    async (conversationId: string, content: string) => {
      if (!token || isStreaming) return

      activeConvRef.current = conversationId
      setActiveConversationId(conversationId)
      lastTitleRef.current = null
      setLatestTitle(null)
      const userMsg: ChatMessage = { role: 'user', content }
      setMessages((prev) => [...prev, userMsg])
      setIsStreaming(true)
      setCurrentToolCall(null)

      const controller = new AbortController()
      abortRef.current = controller

      const assistantMsg: ChatMessage = {
        role: 'assistant',
        content: '',
        tool_calls: [],
        isStreaming: true,
      }
      setMessages((prev) => [...prev, assistantMsg])

      // Helper: only update messages if this stream's conversation is still active.
      // If the user navigated to a different conversation, skip the update silently —
      // the stream result will be loaded from the API when they navigate back.
      const scopedSetMessages = (updater: (prev: ChatMessage[]) => ChatMessage[]) => {
        setMessages((prev) => {
          // Check at update time whether context still belongs to this stream
          if (activeConvRef.current !== conversationId) return prev
          return updater(prev)
        })
      }

      try {
        const res = await fetch(`/api/chat/conversations/${conversationId}/messages`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
          },
          body: JSON.stringify({ content }),
          signal: controller.signal,
        })

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
                  scopedSetMessages((prev) => {
                    const updated = [...prev]
                    const last = updated[updated.length - 1]
                    if (last && last.role === 'assistant') {
                      updated[updated.length - 1] = { ...last, rag_context: event.chunks }
                    }
                    return updated
                  })
                  break

                case 'token':
                  scopedSetMessages((prev) => {
                    const updated = [...prev]
                    const last = updated[updated.length - 1]
                    if (last && last.role === 'assistant') {
                      updated[updated.length - 1] = { ...last, content: last.content + event.content }
                    }
                    return updated
                  })
                  break

                case 'tool_call':
                  if (activeConvRef.current === conversationId) {
                    setCurrentToolCall({ name: event.name, arguments: event.arguments })
                  }
                  break

                case 'tool_result':
                  scopedSetMessages((prev) => {
                    const updated = [...prev]
                    const last = updated[updated.length - 1]
                    if (last && last.role === 'assistant') {
                      updated[updated.length - 1] = {
                        ...last,
                        tool_calls: [
                          ...(last.tool_calls || []),
                          { name: event.name, arguments: {}, result: event.summary },
                        ],
                      }
                    }
                    return updated
                  })
                  if (activeConvRef.current === conversationId) {
                    setCurrentToolCall(null)
                  }
                  break

                case 'title':
                  if (event.title) {
                    lastTitleRef.current = event.title
                    setLatestTitle(event.title)
                  }
                  break

                case 'done':
                  if (event.title) {
                    lastTitleRef.current = event.title
                    setLatestTitle(event.title)
                  }
                  scopedSetMessages((prev) => {
                    const updated = [...prev]
                    const last = updated[updated.length - 1]
                    if (last && last.role === 'assistant') {
                      updated[updated.length - 1] = {
                        ...last,
                        isStreaming: false,
                        model_id: event.model_id || last.model_id,
                        context_pct: event.context_pct,
                      }
                    }
                    return updated
                  })
                  break

                case 'error':
                  scopedSetMessages((prev) => {
                    const updated = [...prev]
                    const last = updated[updated.length - 1]
                    if (last && last.role === 'assistant') {
                      updated[updated.length - 1] = {
                        ...last,
                        content: `Error: ${event.message}`,
                        errorDetail: event.detail || undefined,
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
        scopedSetMessages((prev) => {
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

  return (
    <ChatContext.Provider
      value={{
        activeConversationId,
        messages,
        isStreaming,
        currentToolCall,
        latestTitle,
        sendMessage,
        stopStreaming,
        loadMessages,
        setMessages,
        lastTitle: lastTitleRef,
      }}
    >
      {children}
    </ChatContext.Provider>
  )
}

export function useChat(): ChatState {
  const ctx = useContext(ChatContext)
  if (!ctx) throw new Error('useChat must be used within ChatProvider')
  return ctx
}
