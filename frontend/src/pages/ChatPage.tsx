import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { del, get, post } from '../api'
import { useAuth } from '../auth'
import { useChat, type ChatMessage, type RagContextChunk, type ToolCallInfo } from '../contexts/ChatContext'
import RagContextCard from '../components/RagContextCard'

interface ConversationSummary {
  conversation_id: string
  title: string
  created_at: string
  updated_at: string
}

interface ConversationDetail extends ConversationSummary {
  messages: {
    message_id: string
    role: string
    content: string
    tool_calls?: unknown[]
    tool_call_id?: string
    rag_context?: RagContextChunk[]
    tokens_used?: number
    model_id?: string
    context_pct?: number
    created_at: string
  }[]
}

interface ModelInfo {
  id: string
  name: string
  active: boolean
  downloaded: boolean
  size_bytes: number
}

function formatRelativeDate(dateStr: string): string {
  const d = new Date(dateStr)
  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffMins = Math.floor(diffMs / 60000)
  if (diffMins < 1) return 'just now'
  if (diffMins < 60) return `${diffMins}m ago`
  const diffHours = Math.floor(diffMins / 60)
  if (diffHours < 24) return `${diffHours}h ago`
  const diffDays = Math.floor(diffHours / 24)
  if (diffDays < 7) return `${diffDays}d ago`
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export default function ChatPage() {
  const { conversationId } = useParams<{ conversationId?: string }>()
  const navigate = useNavigate()
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [input, setInput] = useState('')
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const scrollContainerRef = useRef<HTMLDivElement>(null)

  const { token } = useAuth()
  const {
    activeConversationId: chatCtxConvId,
    messages,
    isStreaming,
    currentToolCall,
    sendMessage,
    stopStreaming,
    loadMessages,
    lastTitle,
    latestTitle,
  } = useChat()
  const [modelNames, setModelNames] = useState<Record<string, string>>({})
  const [hasActiveModel, setHasActiveModel] = useState(true) // optimistic default
  const [activeModelId, setActiveModelId] = useState<string | null>(null)
  const [researchActive, setResearchActive] = useState(false)

  // Derive latest context_pct from most recent assistant message
  const latestContextPct = [...messages]
    .reverse()
    .find((m) => m.role === 'assistant' && m.context_pct != null)?.context_pct
  const contextFull = (latestContextPct ?? 0) >= 95

  useEffect(() => {
    const check = async () => {
      try {
        const res = await fetch('/api/research/active', {
          headers: { Authorization: `Bearer ${token}` },
        })
        if (res.ok) {
          const data = await res.json()
          setResearchActive(data.active)
        }
      } catch {
        // Ignore — research endpoint may not exist yet
      }
    }
    check()
    // Poll every 10s so the blocker clears when research finishes
    const interval = setInterval(check, 10000)
    return () => clearInterval(interval)
  }, [token])

  useEffect(() => {
    get<ConversationSummary[]>('/api/chat/conversations')
      .then(setConversations)
      .catch(() => {})
    get<ModelInfo[]>('/api/chat/models')
      .then((models) => {
        const map: Record<string, string> = {}
        let activeId: string | null = null
        for (const m of models) {
          map[m.id] = m.name
          if (m.active) activeId = m.id
        }
        setModelNames(map)
        setActiveModelId(activeId)
        setHasActiveModel(activeId !== null)
      })
      .catch(() => {})
  }, [])

  // Update sidebar title immediately when the backend sends a title event
  useEffect(() => {
    async function updateTitle() {
      if (latestTitle && conversationId) {
        setConversations((prev) =>
          prev.map((c) => (c.conversation_id === conversationId ? { ...c, title: latestTitle } : c)),
        )
      }
    }
    updateTitle()
  }, [latestTitle, conversationId])

  useEffect(() => {
    if (!conversationId) {
      // Only clear if context isn't actively streaming (avoid wiping mid-stream state
      // during the brief moment before navigate replaces the URL)
      if (!isStreaming) loadMessages('', [])
      return
    }
    // If the context already has messages for this conversation (e.g. navigated away and back
    // during streaming, or just created this conversation), skip reloading.
    if (chatCtxConvId === conversationId) return
    // If a stream is in-flight for a different conversation, don't clobber it —
    // just load this conversation's messages from the API into context.
    // The stream's scoped updates will be no-ops since activeConversationId changed.
    get<ConversationDetail>(`/api/chat/conversations/${conversationId}`)
      .then((conv) => {
        loadMessages(
          conversationId,
          conv.messages
            .filter((m) => m.role !== 'tool')
            .filter((m) => m.role !== 'assistant' || m.content || (m.tool_calls && m.tool_calls.length > 0))
            .map((m) => ({
              message_id: m.message_id,
              role: m.role as ChatMessage['role'],
              content: m.content,
              tool_calls: (m.tool_calls as ToolCallInfo[] | undefined) || undefined,
              rag_context: m.rag_context,
              model_id: m.model_id || undefined,
              context_pct: m.context_pct,
            })),
        )
      })
      .catch(() => {
        navigate('/')
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, currentToolCall])

  const handleSubmit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault()
      const text = inputRef.current?.value.trim() ?? ''
      if (!text || isStreaming || !hasActiveModel || contextFull) return
      setInput('')

      // Reset textarea height
      if (inputRef.current) {
        inputRef.current.style.height = 'auto'
      }

      let activeConvId = conversationId
      if (!activeConvId) {
        // Use the user's query as the title immediately (truncated)
        const eagerTitle = text.length > 80 ? text.slice(0, 77) + '...' : text
        const conv = await post<ConversationSummary>('/api/chat/conversations', {
          title: eagerTitle,
        })
        activeConvId = conv.conversation_id
        setConversations((prev) => [conv, ...prev])
        // Navigate to the new conversation — context preserves streaming state across remount
        navigate(`/c/${activeConvId}`, { replace: true })
      }

      await sendMessage(activeConvId, text, activeModelId || undefined).finally(() => {
        if (lastTitle.current && activeConvId) {
          setConversations((prev) =>
            prev.map((c) => (c.conversation_id === activeConvId ? { ...c, title: lastTitle.current! } : c)),
          )
        }
        get<ConversationSummary[]>('/api/chat/conversations')
          .then(setConversations)
          .catch(() => {})
      })
    },
    [isStreaming, conversationId, sendMessage, lastTitle, hasActiveModel, activeModelId, contextFull],
  )

  const handleNewChat = useCallback(() => {
    if (isStreaming) stopStreaming()
    loadMessages('', [])
    navigate('/')
    inputRef.current?.focus()
  }, [isStreaming, stopStreaming, loadMessages, navigate])

  const handleDeleteConversation = useCallback(
    async (convId: string) => {
      await del(`/api/chat/conversations/${convId}`)
      setConversations((prev) => prev.filter((c) => c.conversation_id !== convId))
      if (conversationId === convId) {
        loadMessages('', [])
        navigate('/')
      }
    },
    [conversationId, loadMessages, navigate],
  )

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        handleSubmit(e as unknown as FormEvent)
      }
    },
    [handleSubmit],
  )

  const activeConvTitle = conversationId
    ? conversations.find((c) => c.conversation_id === conversationId)?.title
    : undefined

  return (
    <div className="chat-page flex h-[calc(100vh-3.5rem)] -mx-4 -my-6 overflow-hidden">
      {/* Sidebar */}
      <div
        className={`chat-sidebar shrink-0 flex flex-col border-r border-gray-200/80 dark:border-gray-700/60 bg-stone-50 dark:bg-gray-900/80 transition-all duration-300 ease-in-out ${
          sidebarOpen ? 'w-72' : 'w-0'
        } overflow-hidden`}
      >
        <div className="p-3 pb-2">
          <button
            onClick={handleNewChat}
            className="group w-full flex items-center gap-2.5 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-3.5 py-2.5 text-[13px] font-medium text-gray-600 dark:text-gray-300 shadow-xs hover:shadow-md hover:border-gray-300 dark:hover:border-gray-600 transition-all duration-200"
          >
            <span className="flex h-5 w-5 items-center justify-center rounded-md bg-gray-800 dark:bg-gray-200 text-white dark:text-gray-800 text-xs transition-transform duration-200 group-hover:scale-110">
              +
            </span>
            New conversation
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-2 pb-2 chat-sidebar-scroll">
          {conversations.length === 0 && (
            <div className="px-3 py-8 text-center text-xs text-gray-400 dark:text-gray-500">No conversations yet</div>
          )}
          {conversations.map((conv) => {
            const isActive = conv.conversation_id === conversationId
            return (
              <div
                key={conv.conversation_id}
                className={`group relative flex items-start rounded-lg px-3 py-2.5 mb-0.5 cursor-pointer transition-all duration-150 ${
                  isActive
                    ? 'bg-white dark:bg-gray-800 shadow-xs ring-1 ring-gray-200/80 dark:ring-gray-700/60'
                    : 'hover:bg-white/60 dark:hover:bg-gray-800/40'
                }`}
              >
                <Link to={`/c/${conv.conversation_id}`} className="flex-1 min-w-0">
                  <div
                    className={`text-[13px] font-medium truncate ${
                      isActive ? 'text-gray-900 dark:text-gray-100' : 'text-gray-600 dark:text-gray-400'
                    }`}
                  >
                    {conv.title}
                  </div>
                  <div className="text-[11px] text-gray-400 dark:text-gray-500 mt-0.5">
                    {formatRelativeDate(conv.updated_at)}
                  </div>
                </Link>
                <button
                  onClick={(e) => {
                    e.preventDefault()
                    e.stopPropagation()
                    handleDeleteConversation(conv.conversation_id)
                  }}
                  className="absolute right-2 top-2.5 rounded-md p-1 text-gray-300 dark:text-gray-600 opacity-0 group-hover:opacity-100 hover:text-red-500 dark:hover:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 transition-all duration-150"
                  title="Delete conversation"
                >
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
                    />
                  </svg>
                </button>
              </div>
            )
          })}
        </div>
      </div>

      {/* Main chat area */}
      <div className="relative flex flex-1 flex-col min-w-0 bg-white dark:bg-gray-900">
        {/* Header */}
        <div className="flex items-center gap-3 border-b border-gray-100 dark:border-gray-800 px-4 py-2.5 bg-white dark:bg-gray-900">
          <button
            onClick={() => setSidebarOpen(!sidebarOpen)}
            className="rounded-lg p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors duration-150"
            title={sidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
          >
            <svg className="h-4.5 w-4.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              {sidebarOpen ? (
                <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12H12m-8.25 5.25h16.5" />
              ) : (
                <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
              )}
            </svg>
          </button>
          {activeConvTitle ? (
            <h2 className="text-[13px] font-semibold text-gray-700 dark:text-gray-300 truncate">{activeConvTitle}</h2>
          ) : (
            <h2 className="text-[13px] font-medium text-gray-400 dark:text-gray-500">New conversation</h2>
          )}
          <div className="ml-auto flex items-center gap-2">
            {isStreaming && (
              <div className="flex items-center gap-1.5">
                <div className="streaming-dots flex gap-0.5">
                  <span className="h-1 w-1 rounded-full bg-blue-500" />
                  <span className="h-1 w-1 rounded-full bg-blue-500" />
                  <span className="h-1 w-1 rounded-full bg-blue-500" />
                </div>
                <span className="text-[11px] text-gray-400">Generating</span>
              </div>
            )}
            {conversationId && !isStreaming && messages.length > 0 && (
              <button
                onClick={() => {
                  if (!token || !conversationId) return
                  const a = document.createElement('a')
                  a.href = `/api/chat/conversations/${conversationId}/export`
                  a.download = ''
                  // Need auth header — use fetch instead
                  fetch(`/api/chat/conversations/${conversationId}/export`, {
                    headers: { Authorization: `Bearer ${token}` },
                  })
                    .then((r) => r.blob())
                    .then((blob) => {
                      const url = URL.createObjectURL(blob)
                      const link = document.createElement('a')
                      link.href = url
                      link.download = `${activeConvTitle || 'conversation'}.md`
                      link.click()
                      URL.revokeObjectURL(url)
                    })
                    .catch(() => {})
                }}
                className="rounded-lg p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors duration-150"
                title="Download transcript"
              >
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"
                  />
                </svg>
              </button>
            )}
          </div>
        </div>

        {/* Messages */}
        <div ref={scrollContainerRef} className="flex-1 overflow-y-auto chat-messages-scroll">
          {messages.length === 0 ? (
            !hasActiveModel ? (
              <ModelNudge />
            ) : researchActive ? (
              <div className="flex h-full items-center justify-center p-8">
                <div className="text-center max-w-md empty-state-appear">
                  <div className="mb-4">
                    <img src="/research-octopus.png" alt="" className="h-48 mx-auto opacity-60" />
                  </div>
                  <h3 className="text-[15px] font-semibold text-gray-800 dark:text-gray-200 mb-1.5">
                    Research in progress
                  </h3>
                  <p className="text-[13px] text-gray-400 dark:text-gray-500 leading-relaxed">
                    A research task is running.{' '}
                    <a href="/research" className="text-amber-600 dark:text-amber-400 underline hover:no-underline">
                      View progress
                    </a>
                  </p>
                </div>
              </div>
            ) : (
              <EmptyState />
            )
          ) : (
            <div className="mx-auto px-6 py-6 space-y-1" style={{ maxWidth: 'min(100%, 72rem)' }}>
              {messages.map((msg, i) => (
                <MessageBubble key={msg.message_id || i} message={msg} modelNames={modelNames} />
              ))}

              {currentToolCall && <ToolCallCard tool={currentToolCall} active />}

              <div ref={messagesEndRef} />
            </div>
          )}
        </div>

        {/* Input area */}
        {researchActive && (
          <div className="flex items-center gap-2 border-t border-amber-200 dark:border-amber-800 bg-amber-50/80 dark:bg-amber-900/20 px-4 py-2">
            <span className="text-[13px] text-amber-700 dark:text-amber-300">
              Research task in progress —{' '}
              <a href="/research" className="underline hover:no-underline">
                view in Research tab
              </a>
            </span>
          </div>
        )}
        <div className="border-t border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900">
          <div className="mx-auto px-6 py-3" style={{ maxWidth: 'min(100%, 72rem)' }}>
            <form onSubmit={handleSubmit} className="relative">
              <div className="chat-input-container relative rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-800/50 shadow-xs focus-within:shadow-md focus-within:border-gray-300 dark:focus-within:border-gray-600 transition-all duration-200">
                {activeModelId && modelNames[activeModelId] && (
                  <div className="absolute top-1.5 right-3 text-[10px] text-gray-300 dark:text-gray-600 select-none pointer-events-none">
                    {modelNames[activeModelId]}
                  </div>
                )}
                <textarea
                  ref={inputRef}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder={
                    !hasActiveModel
                      ? 'Download and activate a model to start chatting'
                      : contextFull
                        ? 'Context is nearly full — start a new conversation'
                        : researchActive
                          ? 'Research task in progress...'
                          : 'Ask about your documents...'
                  }
                  disabled={!hasActiveModel || researchActive || contextFull}
                  rows={1}
                  className={`w-full resize-none border-0 bg-transparent px-4 pt-3 pb-2 text-sm text-gray-800 dark:text-gray-200 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-hidden${!hasActiveModel || researchActive || contextFull ? ' opacity-50 pointer-events-none' : ''}`}
                  style={{ maxHeight: '160px' }}
                  onInput={(e) => {
                    const target = e.target as HTMLTextAreaElement
                    target.style.height = 'auto'
                    target.style.height = Math.min(target.scrollHeight, 160) + 'px'
                  }}
                />
                <div className="flex items-center justify-between px-3 pb-2">
                  <span className="text-[10px] text-gray-300 dark:text-gray-600 select-none">
                    {input.trim() ? 'Enter to send' : 'Shift+Enter for new line'}
                  </span>
                  <div className="flex items-center gap-2">
                    {isStreaming ? (
                      <button
                        type="button"
                        onClick={stopStreaming}
                        className="flex items-center gap-1.5 rounded-lg bg-gray-800 dark:bg-gray-200 px-3 py-1.5 text-xs font-medium text-white dark:text-gray-800 hover:bg-gray-700 dark:hover:bg-gray-300 transition-colors duration-150"
                      >
                        <svg className="h-3 w-3" viewBox="0 0 24 24" fill="currentColor">
                          <rect x="6" y="6" width="12" height="12" rx="2" />
                        </svg>
                        Stop
                      </button>
                    ) : (
                      <button
                        type="submit"
                        disabled={!input.trim() || !hasActiveModel || researchActive || contextFull}
                        className="flex items-center justify-center rounded-lg bg-gray-800 dark:bg-gray-200 p-1.5 text-white dark:text-gray-800 hover:bg-gray-700 dark:hover:bg-gray-300 disabled:opacity-30 disabled:hover:bg-gray-800 dark:disabled:hover:bg-gray-200 transition-all duration-150"
                      >
                        <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 10.5L12 3m0 0l7.5 7.5M12 3v18" />
                        </svg>
                      </button>
                    )}
                  </div>
                </div>
              </div>
            </form>
          </div>
        </div>
      </div>
    </div>
  )
}

/* ---- Model onboarding nudge ---- */

function ModelNudge() {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="text-center max-w-lg empty-state-appear">
        <div className="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-2xl bg-amber-50 dark:bg-amber-900/30 ring-1 ring-amber-200/60 dark:ring-amber-700/40">
          <svg
            className="h-8 w-8 text-amber-500 dark:text-amber-400"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={1.5}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456z"
            />
          </svg>
        </div>
        <h3 className="text-lg font-semibold text-gray-800 dark:text-gray-200 mb-2">Set up your local AI model</h3>
        <p className="text-[13px] text-gray-500 dark:text-gray-400 leading-relaxed mb-2">
          Harbor Clerk runs a local LLM to chat with your documents. Download a model to get started — everything stays
          on this machine.
        </p>
        <p className="text-[13px] text-gray-500 dark:text-gray-400 leading-relaxed mb-6">
          We recommend <strong className="text-gray-700 dark:text-gray-300">Qwen3 8B</strong> as a great starting point
          — strong reasoning, tool use, and bilingual support.
        </p>
        <Link
          to="/admin/models"
          className="inline-flex items-center gap-2 rounded-xl bg-blue-600 px-5 py-2.5 text-sm font-medium text-white shadow-xs hover:bg-blue-700 transition-colors"
        >
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"
            />
          </svg>
          Choose a model
        </Link>
      </div>
    </div>
  )
}

/* ---- Empty state ---- */

function EmptyState() {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="text-center max-w-md empty-state-appear">
        <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-2xl bg-gray-100 dark:bg-gray-800">
          <svg
            className="h-7 w-7 text-gray-400 dark:text-gray-500"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={1.2}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m5.231 13.481L15 17.25m-4.5-15H5.625c-.621 0-1.125.504-1.125 1.125v16.5c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9zm3.75 11.625a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z"
            />
          </svg>
        </div>
        <h3 className="text-[15px] font-semibold text-gray-800 dark:text-gray-200 mb-1.5">Ask your documents</h3>
        <p className="text-[13px] text-gray-400 dark:text-gray-500 leading-relaxed mb-6">
          Start a conversation to search, read, and reason over your document library using a local LLM.
        </p>
        <div className="flex flex-wrap justify-center gap-2">
          {['What documents mention compliance?', 'Summarize the latest report', 'Find conflicting information'].map(
            (q) => (
              <span
                key={q}
                className="inline-block rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-1.5 text-[12px] text-gray-500 dark:text-gray-400 shadow-xs cursor-default"
              >
                {q}
              </span>
            ),
          )}
        </div>
      </div>
    </div>
  )
}

/* ---- Thinking/reasoning parser ---- */

function parseThinking(content: string): { thinking: string | null; response: string } {
  // Handle streaming: <think> opened but not closed yet
  if (content.startsWith('<think>') && !content.includes('</think>')) {
    return { thinking: content.slice(7), response: '' }
  }
  const match = content.match(/^<think>([\s\S]*?)<\/think>\s*/)
  if (match) {
    return { thinking: match[1].trim(), response: content.slice(match[0].length) }
  }
  return { thinking: null, response: content }
}

function ThinkingSection({
  thinking,
  isStreaming,
  hasResponse,
}: {
  thinking: string
  isStreaming?: boolean
  hasResponse: boolean
}) {
  const ref = useRef<HTMLDetailsElement>(null)
  const userToggled = useRef(false)
  const lineCount = thinking.split('\n').filter((l) => l.trim()).length

  // Auto-open while actively thinking (no response yet), auto-close when response starts
  useEffect(() => {
    if (userToggled.current || !ref.current) return
    ref.current.open = !!isStreaming && !hasResponse
  }, [isStreaming, hasResponse])

  return (
    <details
      ref={ref}
      onToggle={() => {
        userToggled.current = true
      }}
      className="mb-2 group/thinking"
    >
      <summary className="flex items-center gap-1.5 cursor-pointer text-[11px] font-medium text-gray-400 dark:text-gray-500 select-none hover:text-gray-500 dark:hover:text-gray-400 transition-colors">
        <svg className="h-3 w-3 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z"
          />
        </svg>
        <span>Reasoning</span>
        <span className="text-gray-300 dark:text-gray-600">
          ({lineCount} {lineCount === 1 ? 'line' : 'lines'})
        </span>
      </summary>
      <div className="mt-1.5 max-h-48 overflow-y-auto rounded-md bg-gray-100/50 dark:bg-gray-900/40 px-3 py-2 text-xs text-gray-400 dark:text-gray-500 italic whitespace-pre-wrap">
        {thinking}
      </div>
    </details>
  )
}

/* ---- Message bubble ---- */

function MessageBubble({ message, modelNames }: { message: ChatMessage; modelNames: Record<string, string> }) {
  const isUser = message.role === 'user'

  // Skip empty assistant messages from multi-round tool calling history
  if (
    !isUser &&
    !message.content &&
    !message.isStreaming &&
    (!message.tool_calls || message.tool_calls.length === 0) &&
    (!message.rag_context || message.rag_context.length === 0)
  ) {
    return null
  }

  const isError = !isUser && message.content.startsWith('Error:')
  const { thinking, response } = !isUser
    ? parseThinking(message.content)
    : { thinking: null, response: message.content }

  const modelLabel = !isUser && message.model_id ? modelNames[message.model_id] || message.model_id : null

  return (
    <div className={`message-appear py-2.5 ${isUser ? '' : ''}`}>
      <div className={`flex gap-3 ${isUser ? 'flex-row-reverse' : ''}`}>
        {/* Avatar */}
        <div
          className={`shrink-0 mt-0.5 h-7 w-7 rounded-lg flex items-center justify-center text-xs font-semibold ${
            isUser
              ? 'bg-gray-800 dark:bg-gray-200 text-white dark:text-gray-800'
              : 'bg-amber-50 dark:bg-amber-900/30 text-amber-600 dark:text-amber-400 ring-1 ring-amber-200/60 dark:ring-amber-700/40'
          }`}
        >
          {isUser ? (
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0A17.933 17.933 0 0112 21.75c-2.676 0-5.216-.584-7.499-1.632z"
              />
            </svg>
          ) : (
            <img src="/favicon.svg" alt="" className="h-4 w-4" />
          )}
        </div>

        {/* Content */}
        <div className={`min-w-0 max-w-[92%] ${isUser ? 'text-right' : ''}`}>
          {/* Role label */}
          <div
            className={`text-[11px] font-medium mb-1 ${
              isUser ? 'text-gray-400 dark:text-gray-500 mr-1' : 'text-gray-400 dark:text-gray-500 ml-1'
            }`}
          >
            {isUser ? 'You' : 'Harbor Clerk'}
            {modelLabel && <span className="ml-1 font-normal text-gray-300 dark:text-gray-600">({modelLabel})</span>}
            {!isUser && message.context_pct != null && (
              <span
                className={`ml-1.5 text-[12px] font-medium ${
                  message.context_pct >= 85
                    ? 'text-red-500 dark:text-red-400'
                    : message.context_pct >= 65
                      ? 'text-amber-500 dark:text-amber-400'
                      : 'text-gray-400 dark:text-gray-500'
                }`}
                title={`${message.context_pct}% of model context window used`}
              >
                · {message.context_pct}% context
              </span>
            )}
          </div>

          {/* RAG context card shown above the message bubble */}
          {!isUser && message.rag_context && message.rag_context.length > 0 && (
            <div className="mb-1.5">
              <RagContextCard chunks={message.rag_context} />
            </div>
          )}

          <div
            className={`rounded-xl px-4 py-2.5 text-[13.5px] leading-relaxed ${
              isUser
                ? 'bg-gray-800 dark:bg-gray-700 text-gray-100 dark:text-gray-200 rounded-tr-sm'
                : 'bg-gray-50 dark:bg-gray-800/60 text-gray-700 dark:text-gray-300 ring-1 ring-gray-100 dark:ring-gray-700/50 rounded-tl-sm'
            }`}
          >
            {/* Tool calls shown as inline cards */}
            {message.tool_calls && message.tool_calls.length > 0 && (
              <div className="mb-2.5 space-y-1.5">
                {message.tool_calls.map((tc, i) => (
                  <ToolCallCard key={i} tool={tc} active={false} />
                ))}
              </div>
            )}

            {/* Thinking/reasoning section */}
            {thinking && (
              <ThinkingSection
                thinking={thinking}
                isStreaming={message.isStreaming}
                hasResponse={response.length > 0}
              />
            )}

            {/* Main response content */}
            {response &&
              (isUser ? (
                <div className="whitespace-pre-wrap wrap-break-word">{response}</div>
              ) : (
                <div className="prose-chat">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{response}</ReactMarkdown>
                </div>
              ))}

            {/* Error detail disclosure */}
            {isError && message.errorDetail && (
              <details className="mt-2">
                <summary className="cursor-pointer text-[11px] text-gray-400 dark:text-gray-500 hover:text-gray-500 dark:hover:text-gray-400 select-none">
                  Show details
                </summary>
                <pre className="mt-1 max-h-40 overflow-auto rounded-md bg-gray-100 dark:bg-gray-900/60 px-3 py-2 text-[11px] text-gray-400 dark:text-gray-500 font-mono whitespace-pre-wrap">
                  {message.errorDetail}
                </pre>
              </details>
            )}

            {message.isStreaming && !message.content && (
              <div className="flex items-center gap-1.5 py-0.5">
                <div className="thinking-dots flex gap-0.5">
                  <span className="h-1.5 w-1.5 rounded-full bg-gray-300 dark:bg-gray-600" />
                  <span className="h-1.5 w-1.5 rounded-full bg-gray-300 dark:bg-gray-600" />
                  <span className="h-1.5 w-1.5 rounded-full bg-gray-300 dark:bg-gray-600" />
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

/* ---- Tool call card ---- */

function ToolCallCard({ tool, active }: { tool: ToolCallInfo; active: boolean }) {
  const [expanded, setExpanded] = useState(false)

  const icon =
    tool.name === 'search_documents' ? (
      <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"
        />
      </svg>
    ) : tool.name === 'read_passages' ? (
      <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M12 6.042A8.967 8.967 0 006 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 016 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 016-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0018 18a8.967 8.967 0 00-6 2.292m0-14.25v14.25"
        />
      </svg>
    ) : (
      <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M11.42 15.17l-5.1-5.1m0 0L11.42 4.97m-5.1 5.1H20.8" />
      </svg>
    )

  const label =
    tool.name === 'search_documents'
      ? 'Searching documents'
      : tool.name === 'read_passages'
        ? 'Reading passages'
        : tool.name

  return (
    <div
      className={`tool-call-card rounded-lg text-xs overflow-hidden transition-all duration-200 ${
        active
          ? 'bg-blue-50/80 dark:bg-blue-900/10 ring-1 ring-blue-200/60 dark:ring-blue-800/30'
          : 'bg-white/60 dark:bg-gray-700/30 ring-1 ring-gray-200/60 dark:ring-gray-600/30'
      }`}
    >
      <button onClick={() => setExpanded(!expanded)} className="flex w-full items-center gap-2 px-2.5 py-1.5">
        <span
          className={`shrink-0 ${
            active ? 'text-blue-500 dark:text-blue-400 animate-pulse' : 'text-emerald-500 dark:text-emerald-400'
          }`}
        >
          {active ? (
            icon
          ) : (
            <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
            </svg>
          )}
        </span>
        <span className="font-medium text-gray-600 dark:text-gray-300">
          {label}
          {active ? '...' : ''}
        </span>
        {tool.result && (
          <span className="ml-auto text-gray-400 dark:text-gray-500 truncate max-w-[200px]">{tool.result}</span>
        )}
        <svg
          className={`h-3 w-3 shrink-0 text-gray-300 dark:text-gray-600 transition-transform duration-150 ${expanded ? 'rotate-180' : ''}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
        </svg>
      </button>
      {expanded && (
        <div className="border-t border-gray-100 dark:border-gray-700/50 px-2.5 py-1.5 text-[11px] bg-gray-50/50 dark:bg-gray-800/30 space-y-1">
          {tool.arguments && Object.keys(tool.arguments).length > 0 && (
            <div className="font-mono text-gray-400 dark:text-gray-500">{JSON.stringify(tool.arguments, null, 2)}</div>
          )}
          {tool.result && <div className="text-gray-500 dark:text-gray-400">{tool.result}</div>}
        </div>
      )}
    </div>
  )
}
