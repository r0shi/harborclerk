import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import CitedMarkdown from '../components/CitedMarkdown'
import { IconTile } from '../components/IconTile'
import { FolderPicker } from '../components/FolderPicker'
import { LocalAISetupPrompt } from '../components/LocalAISetupPrompt'
import { LocalModelChip } from '../components/LocalModelChip'
import { ScopeChip } from '../components/ScopeChip'
import { del, get, post } from '../api'
import { useAuth } from '../auth'
import { useChat, type ChatMessage, type RagContextChunk, type ToolCallInfo } from '../contexts/ChatContext'
import { useResearch } from '../contexts/ResearchContext'
import { useWatchedFolders } from '../hooks/useWatchedFolders'
import RagContextCard from '../components/RagContextCard'
import ToolResultDisplay from '../components/ToolResultDisplay'
import { formatRelativeDate } from '../utils/dates'
import { topicDotVar } from '../utils/topicDotColor'

interface ConversationSummary {
  conversation_id: string
  title: string
  created_at: string
  updated_at: string
  scope?: { folder_ids?: string[] }
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
  supports_research: boolean
}

export default function ChatPage() {
  const { conversationId } = useParams<{ conversationId?: string }>()
  const navigate = useNavigate()
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [input, setInputRaw] = useState(() => sessionStorage.getItem('chat_draft') ?? '')
  const setInput = useCallback((v: string) => {
    setInputRaw(v)
    if (v) sessionStorage.setItem('chat_draft', v)
    else sessionStorage.removeItem('chat_draft')
  }, [])
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
  const { conversationId: researchConversationId } = useResearch()
  const { folders } = useWatchedFolders()
  const [modelNames, setModelNames] = useState<Record<string, string>>({})
  const [hasActiveModel, setHasActiveModel] = useState(true) // optimistic default
  const [activeModelId, setActiveModelId] = useState<string | null>(null)
  const [activeModel, setActiveModel] = useState<ModelInfo | null>(null)
  const [researchActive, setResearchActive] = useState(false)
  // Folder scope for next new conversation (reset when conversation is created)
  const [newConvFolderIds, setNewConvFolderIds] = useState<string[]>([])

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
        let active: ModelInfo | null = null
        for (const m of models) {
          map[m.id] = m.name
          if (m.active) {
            activeId = m.id
            active = m
          }
        }
        setModelNames(map)
        setActiveModelId(activeId)
        setActiveModel(active)
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
              tool_calls: m.tool_calls
                ? (m.tool_calls as Array<Record<string, unknown>>).map((tc) => ({
                    name: tc.name as string,
                    arguments: (tc.arguments as Record<string, unknown>) || {},
                    result: tc.result as string | undefined,
                    rawResult: tc.raw_result as string | undefined,
                  }))
                : undefined,
              rag_context: m.rag_context,
              model_id: m.model_id || undefined,
              context_pct: m.context_pct,
            })),
        )
      })
      .catch(() => {
        navigate('/ask')
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

      if (inputRef.current) {
        inputRef.current.style.height = 'auto'
      }

      try {
        let activeConvId = conversationId
        if (!activeConvId) {
          const eagerTitle = text.length > 80 ? text.slice(0, 77) + '...' : text
          const scopePayload = newConvFolderIds.length > 0 ? { scope: { folder_ids: newConvFolderIds } } : {}
          const conv = await post<ConversationSummary>('/api/chat/conversations', {
            title: eagerTitle,
            ...scopePayload,
          })
          activeConvId = conv.conversation_id
          setNewConvFolderIds([])
          setConversations((prev) => [conv, ...prev])
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
      } catch {
        setInput(text)
      }
    },
    [
      isStreaming,
      conversationId,
      sendMessage,
      lastTitle,
      hasActiveModel,
      activeModelId,
      contextFull,
      setInput,
      navigate,
      newConvFolderIds,
    ],
  )

  const handleNewChat = useCallback(() => {
    if (isStreaming) stopStreaming()
    loadMessages('', [])
    navigate('/ask')
    inputRef.current?.focus()
  }, [isStreaming, stopStreaming, loadMessages, navigate])

  const handleDeleteConversation = useCallback(
    async (convId: string) => {
      await del(`/api/chat/conversations/${convId}`)
      setConversations((prev) => prev.filter((c) => c.conversation_id !== convId))
      if (conversationId === convId) {
        loadMessages('', [])
        navigate('/ask')
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

  const activeConv = conversationId ? conversations.find((c) => c.conversation_id === conversationId) : undefined
  const activeConvTitle = activeConv?.title
  const activeConvScope = activeConv?.scope

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
            className="w-full flex items-center gap-1.5 rounded-md px-3 py-2 text-sm font-medium transition-opacity hover:opacity-80"
            style={{
              backgroundColor: 'var(--area-accent-tint)',
              color: 'var(--area-accent-text)',
              border: '1px solid var(--area-accent)',
            }}
          >
            <span aria-hidden>＋</span> New conversation
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
                <Link to={`/c/${conv.conversation_id}`} className="flex-1 min-w-0 flex items-start gap-2">
                  <span
                    className="inline-block mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full"
                    style={{ background: topicDotVar(conv.conversation_id) }}
                  />
                  <div className="min-w-0 flex-1">
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
          {activeConv && (
            <ScopeChip
              scope={activeConvScope ?? {}}
              folders={folders}
              className="shrink-0 inline-flex items-center rounded-md border border-[var(--color-border)] bg-[var(--color-bg-secondary)] px-2 py-0.5 text-[11px] font-medium text-[var(--color-text-secondary)]"
            />
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
              <div className="flex h-full items-center justify-center p-8">
                <LocalAISetupPrompt variant="ask" />
              </div>
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
                    <a
                      href={researchConversationId ? `/research/${researchConversationId}` : '/research'}
                      className="text-amber-600 dark:text-amber-400 underline hover:no-underline"
                    >
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
              <a
                href={researchConversationId ? `/research/${researchConversationId}` : '/research'}
                className="underline hover:no-underline"
              >
                view in Research tab
              </a>
            </span>
          </div>
        )}
        <div className="border-t border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900">
          <div className="mx-auto px-6 py-3" style={{ maxWidth: 'min(100%, 72rem)' }}>
            <form onSubmit={handleSubmit} className="relative">
              {activeModel && (
                <div className="mb-2 flex justify-end">
                  <LocalModelChip model={activeModel} />
                </div>
              )}
              <div className="chat-input-container relative rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-800/50 shadow-xs focus-within:shadow-md focus-within:border-gray-300 dark:focus-within:border-gray-600 transition-all duration-200">
                <textarea
                  ref={inputRef}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder={
                    !hasActiveModel
                      ? 'Choose a local AI model to ask questions'
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
                  <div className="flex items-center gap-2">
                    {!conversationId && (
                      <FolderPicker
                        value={newConvFolderIds}
                        onChange={setNewConvFolderIds}
                        folders={folders}
                        size="sm"
                      />
                    )}
                    {conversationId && (
                      <span className="text-[10px] text-gray-300 dark:text-gray-600 select-none">
                        {input.trim() ? 'Enter to send' : 'Shift+Enter for new line'}
                      </span>
                    )}
                  </div>
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

/* ---- Empty state ---- */

const SUGGESTION_CHIPS = [
  'What documents mention compliance?',
  'Summarize the latest report',
  'Find conflicting information',
]

function EmptyState() {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="flex flex-col items-center justify-center gap-4 py-16 text-center empty-state-appear max-w-md">
        <IconTile size={64}>📚</IconTile>
        <div>
          <h2 className="font-serif text-2xl font-semibold tracking-tight text-(--color-text-primary)">
            Ask your documents
          </h2>
          <p className="mt-2 text-sm text-(--color-text-secondary)">
            Start a conversation to search, read, and reason over your library using local AI.
          </p>
        </div>
        <div className="flex flex-wrap justify-center gap-2">
          {SUGGESTION_CHIPS.map((chip) => (
            <span
              key={chip}
              className="rounded-full px-3 py-1 text-xs cursor-default"
              style={{
                backgroundColor: 'var(--area-accent-tint)',
                color: 'var(--area-accent-text)',
                border: '1px solid var(--area-accent)',
              }}
            >
              {chip}
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}

/* ---- Thinking/reasoning parser ---- */

// Models whose chat template appends "<think>" to the assistant prompt — the
// streamed content starts INSIDE the thinking block with no opening tag, just
// reasoning text, and only emits the closing </think> at the answer boundary.
// Mid-stream we treat early text as thinking until </think> arrives, instead
// of leaking chain-of-thought into the visible reply. Update when adding new
// thinking-by-default models.
const IMPLICIT_THINKING_MODELS = new Set(['deepseek-r1-0528-8b'])

function parseThinking(
  content: string,
  modelId?: string | null,
  isStreaming?: boolean,
): { thinking: string | null; response: string } {
  // Explicit-opening cases: Qwen3 / older patterns that emit <think> inline.
  if (content.startsWith('<think>') && !content.includes('</think>')) {
    return { thinking: content.slice(7), response: '' }
  }
  const match = content.match(/^<think>([\s\S]*?)<\/think>\s*/)
  if (match) {
    return { thinking: match[1].trim(), response: content.slice(match[0].length) }
  }
  // Implicit-opening case: closing </think> arrived but no opening tag was
  // ever emitted. Everything before </think> was reasoning.
  const closeIdx = content.indexOf('</think>')
  if (closeIdx >= 0) {
    return {
      thinking: content.slice(0, closeIdx).trim(),
      response: content.slice(closeIdx + '</think>'.length).trim(),
    }
  }
  // Streaming with a known implicit-thinking model: no </think> yet, so we're
  // still inside the implicit thinking block. Surface the running text in the
  // reasoning section, leaving response empty until the closing tag arrives.
  if (isStreaming && modelId && IMPLICIT_THINKING_MODELS.has(modelId) && content) {
    return { thinking: content, response: '' }
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
    ? parseThinking(message.content, message.model_id, message.isStreaming)
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
                  <CitedMarkdown>{response}</CitedMarkdown>
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
        <div className="border-t border-gray-100 dark:border-gray-700/50 px-2.5 py-1.5 text-[11px] bg-gray-50/50 dark:bg-gray-800/30 space-y-2">
          {tool.arguments && Object.keys(tool.arguments).length > 0 && (
            <div>
              <div className="text-[10px] text-gray-400 uppercase tracking-wide mb-0.5">Arguments</div>
              <div className="font-mono text-gray-400 dark:text-gray-500">
                {JSON.stringify(tool.arguments, null, 2)}
              </div>
            </div>
          )}
          {tool.rawResult ? (
            <div>
              <div className="text-[10px] text-gray-400 uppercase tracking-wide mb-0.5">Result</div>
              <ToolResultDisplay rawResult={tool.rawResult} toolName={tool.name} />
            </div>
          ) : tool.result ? (
            <div className="text-gray-500 dark:text-gray-400">{tool.result}</div>
          ) : null}
        </div>
      )}
    </div>
  )
}
