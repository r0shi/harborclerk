import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { del, get, post } from '../api'
import { useChat, type ChatMessage, type ToolCallInfo } from '../hooks/useChat'

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
    tokens_used?: number
    created_at: string
  }[]
}

export default function ChatPage() {
  const { conversationId } = useParams<{ conversationId?: string }>()
  const navigate = useNavigate()
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [input, setInput] = useState('')
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  const {
    messages,
    isStreaming,
    currentToolCall,
    sendMessage,
    stopStreaming,
    loadMessages,
  } = useChat()

  // Load conversation list
  useEffect(() => {
    get<ConversationSummary[]>('/api/chat/conversations').then(setConversations).catch(() => {})
  }, [])

  // Load conversation messages when ID changes
  useEffect(() => {
    if (!conversationId) {
      loadMessages([])
      return
    }
    get<ConversationDetail>(`/api/chat/conversations/${conversationId}`)
      .then((conv) => {
        loadMessages(
          conv.messages
            .filter((m) => m.role !== 'tool')
            .map((m) => ({
              message_id: m.message_id,
              role: m.role as ChatMessage['role'],
              content: m.content,
            })),
        )
      })
      .catch(() => {
        navigate('/chat')
      })
  }, [conversationId, loadMessages, navigate])

  // Auto-scroll on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, currentToolCall])

  const handleSubmit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault()
      const text = input.trim()
      if (!text || isStreaming) return
      setInput('')

      let activeConvId = conversationId
      if (!activeConvId) {
        // Create new conversation
        const conv = await post<ConversationSummary>('/api/chat/conversations', {
          title: 'New conversation',
        })
        activeConvId = conv.conversation_id
        setConversations((prev) => [conv, ...prev])
        navigate(`/chat/${activeConvId}`, { replace: true })
      }

      await sendMessage(activeConvId, text)

      // Refresh conversation list to update title/timestamp
      get<ConversationSummary[]>('/api/chat/conversations').then(setConversations).catch(() => {})
    },
    [input, isStreaming, conversationId, sendMessage, navigate],
  )

  const handleNewChat = useCallback(() => {
    loadMessages([])
    navigate('/chat')
    inputRef.current?.focus()
  }, [loadMessages, navigate])

  const handleDeleteConversation = useCallback(
    async (convId: string) => {
      await del(`/api/chat/conversations/${convId}`)
      setConversations((prev) => prev.filter((c) => c.conversation_id !== convId))
      if (conversationId === convId) {
        loadMessages([])
        navigate('/chat')
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

  return (
    <div className="flex h-[calc(100vh-3.5rem)] -mx-4 -my-6">
      {/* Sidebar */}
      {sidebarOpen && (
        <div className="w-64 flex-shrink-0 border-r border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 flex flex-col">
          <div className="p-3">
            <button
              onClick={handleNewChat}
              className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-3 py-2 text-sm font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700"
            >
              + New Chat
            </button>
          </div>
          <div className="flex-1 overflow-y-auto">
            {conversations.map((conv) => (
              <div
                key={conv.conversation_id}
                className={`group flex items-center px-3 py-2 text-sm cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-800 ${
                  conv.conversation_id === conversationId
                    ? 'bg-gray-200 dark:bg-gray-700'
                    : ''
                }`}
              >
                <Link
                  to={`/chat/${conv.conversation_id}`}
                  className="flex-1 truncate text-gray-700 dark:text-gray-300"
                >
                  {conv.title}
                </Link>
                <button
                  onClick={(e) => {
                    e.preventDefault()
                    handleDeleteConversation(conv.conversation_id)
                  }}
                  className="ml-1 hidden rounded p-1 text-gray-400 hover:text-red-500 group-hover:block"
                  title="Delete conversation"
                >
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Main chat area */}
      <div className="flex flex-1 flex-col min-w-0">
        {/* Toggle sidebar + header */}
        <div className="flex items-center border-b border-gray-200 dark:border-gray-700 px-4 py-2">
          <button
            onClick={() => setSidebarOpen(!sidebarOpen)}
            className="mr-3 rounded p-1 text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700"
            title={sidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
            </svg>
          </button>
          <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
            {conversationId
              ? conversations.find((c) => c.conversation_id === conversationId)?.title || 'Chat'
              : 'New Chat'}
          </span>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
          {messages.length === 0 && (
            <div className="flex flex-col items-center justify-center h-full text-gray-400 dark:text-gray-500">
              <svg className="h-12 w-12 mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
              </svg>
              <p className="text-sm">Ask a question about your documents</p>
            </div>
          )}

          {messages.map((msg, i) => (
            <MessageBubble key={i} message={msg} />
          ))}

          {currentToolCall && <ToolCallCard tool={currentToolCall} active />}

          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div className="border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-4 py-3">
          <form onSubmit={handleSubmit} className="flex items-end space-x-2">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask a question..."
              rows={1}
              className="flex-1 resize-none rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
              style={{ maxHeight: '120px' }}
              onInput={(e) => {
                const target = e.target as HTMLTextAreaElement
                target.style.height = 'auto'
                target.style.height = Math.min(target.scrollHeight, 120) + 'px'
              }}
            />
            {isStreaming ? (
              <button
                type="button"
                onClick={stopStreaming}
                className="rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700"
              >
                Stop
              </button>
            ) : (
              <button
                type="submit"
                disabled={!input.trim()}
                className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
              >
                Send
              </button>
            )}
          </form>
        </div>
      </div>
    </div>
  )
}

function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user'

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[75%] rounded-lg px-4 py-2.5 text-sm ${
          isUser
            ? 'bg-blue-600 text-white'
            : 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-200'
        }`}
      >
        {/* Tool calls shown as inline cards */}
        {message.tool_calls && message.tool_calls.length > 0 && (
          <div className="mb-2 space-y-1">
            {message.tool_calls.map((tc, i) => (
              <ToolCallCard key={i} tool={tc} active={false} />
            ))}
          </div>
        )}

        {message.content && (
          <div className="whitespace-pre-wrap break-words">{message.content}</div>
        )}

        {message.isStreaming && !message.content && (
          <span className="inline-block animate-pulse">Thinking...</span>
        )}
      </div>
    </div>
  )
}

function ToolCallCard({ tool, active }: { tool: ToolCallInfo; active: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const label =
    tool.name === 'search_documents'
      ? 'Searching documents'
      : tool.name === 'read_passages'
        ? 'Reading passages'
        : tool.name

  return (
    <div
      className={`rounded border text-xs ${
        active
          ? 'border-blue-300 dark:border-blue-600 bg-blue-50 dark:bg-blue-900/20'
          : 'border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-800'
      }`}
    >
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center gap-2 px-3 py-1.5"
      >
        {active ? (
          <div className="h-2 w-2 animate-pulse rounded-full bg-blue-500" />
        ) : (
          <svg className="h-3 w-3 text-green-500" fill="currentColor" viewBox="0 0 20 20">
            <path
              fillRule="evenodd"
              d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z"
              clipRule="evenodd"
            />
          </svg>
        )}
        <span className="font-medium text-gray-700 dark:text-gray-300">
          {label}
          {active ? '...' : ''}
        </span>
        {tool.result && (
          <span className="ml-auto text-gray-500 dark:text-gray-400">{tool.result}</span>
        )}
      </button>
      {expanded && tool.arguments && Object.keys(tool.arguments).length > 0 && (
        <div className="border-t border-gray-200 dark:border-gray-600 px-3 py-1.5 font-mono text-gray-500 dark:text-gray-400">
          {JSON.stringify(tool.arguments)}
        </div>
      )}
    </div>
  )
}
