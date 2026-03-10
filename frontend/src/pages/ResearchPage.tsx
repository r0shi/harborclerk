import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { del, get } from '../api'
import { useResearch, type ToolCallEntry } from '../contexts/ResearchContext'

interface ResearchSummary {
  conversation_id: string
  title: string
  status: string
  strategy: string
  current_round: number
  max_rounds: number
  created_at: string
  completed_at: string | null
}

interface ResearchDetail extends ResearchSummary {
  question: string
  report: string | null
  model_id: string | null
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

function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`
  const mins = Math.floor(seconds / 60)
  const secs = Math.round(seconds % 60)
  return secs > 0 ? `${mins}m ${secs}s` : `${mins}m`
}

function statusColor(status: string): string {
  switch (status) {
    case 'completed':
      return 'bg-emerald-500'
    case 'interrupted':
      return 'bg-amber-500'
    case 'running':
      return 'bg-blue-500'
    case 'failed':
      return 'bg-red-500'
    default:
      return 'bg-gray-400'
  }
}

export default function ResearchPage() {
  const { researchId } = useParams<{ researchId?: string }>()
  const navigate = useNavigate()

  const [history, setHistory] = useState<ResearchSummary[]>([])
  const [selectedTask, setSelectedTask] = useState<ResearchDetail | null>(null)
  const [question, setQuestion] = useState('')
  const [strategy, setStrategy] = useState<'search' | 'sweep'>('search')
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [discardConfirm, setDiscardConfirm] = useState<string | null>(null)
  const discardTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const toolLogRef = useRef<HTMLDivElement>(null)

  const {
    isRunning,
    isSynthesizing,
    progress,
    report,
    error,
    conversationId,
    startResearch,
    resumeResearch,
    cancelResearch,
    reset,
  } = useResearch()

  // Fetch history on mount
  const fetchHistory = useCallback(() => {
    get<ResearchSummary[]>('/api/research')
      .then(setHistory)
      .catch(() => {})
  }, [])

  useEffect(() => {
    fetchHistory()
  }, [fetchHistory])

  // Load task detail when researchId changes
  useEffect(() => {
    async function loadTask() {
      if (!researchId) {
        setSelectedTask(null)
        return
      }
      try {
        const detail = await get<ResearchDetail>(`/api/research/${researchId}`)
        setSelectedTask(detail)
      } catch {
        navigate('/research')
      }
    }
    loadTask()
  }, [researchId, navigate])

  // Refresh sidebar when a research task starts, completes, or errors
  useEffect(() => {
    if (conversationId) {
      fetchHistory()
      if (!isRunning && report) {
        navigate(`/research/${conversationId}`)
      }
    }
  }, [conversationId, isRunning, report, fetchHistory, navigate])

  // Refresh sidebar on error (e.g. 409 conflict reveals blocking task)
  useEffect(() => {
    if (error) fetchHistory()
  }, [error, fetchHistory])

  // Auto-scroll tool log
  useEffect(() => {
    toolLogRef.current?.scrollTo({ top: toolLogRef.current.scrollHeight, behavior: 'smooth' })
  }, [progress?.toolCalls.length])

  // Cleanup discard timer
  useEffect(() => {
    return () => {
      if (discardTimerRef.current) clearTimeout(discardTimerRef.current)
    }
  }, [])

  const handleStartResearch = useCallback(async () => {
    const q = question.trim()
    if (!q) return
    setSelectedTask(null)
    setQuestion('')
    await startResearch(q, strategy)
  }, [question, strategy, startResearch])

  const handleResume = useCallback(
    async (convId: string) => {
      setSelectedTask(null)
      navigate('/research')
      await resumeResearch(convId)
    },
    [resumeResearch, navigate],
  )

  const handleDiscard = useCallback(
    async (convId: string) => {
      if (discardConfirm !== convId) {
        setDiscardConfirm(convId)
        discardTimerRef.current = setTimeout(() => setDiscardConfirm(null), 3000)
        return
      }
      setDiscardConfirm(null)
      await del(`/api/research/${convId}`)
      fetchHistory()
      if (researchId === convId) {
        navigate('/research')
      }
    },
    [discardConfirm, fetchHistory, researchId, navigate],
  )

  const handleNewResearch = useCallback(() => {
    setSelectedTask(null)
    setQuestion('')
    reset()
    navigate('/research')
  }, [navigate, reset])

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        handleStartResearch()
      }
    },
    [handleStartResearch],
  )

  // Determine UI state
  const isIdle = !isRunning && !selectedTask && !error
  const isViewingCompleted = !isRunning && selectedTask?.status === 'completed'
  const isViewingInterrupted = !isRunning && selectedTask?.status === 'interrupted'

  return (
    <div className="flex h-[calc(100vh-3.5rem)] -mx-4 -my-6 overflow-hidden">
      {/* Sidebar */}
      <div
        className={`shrink-0 flex flex-col border-r border-gray-200/80 dark:border-gray-700/60 bg-stone-50 dark:bg-gray-900/80 transition-all duration-300 ease-in-out ${
          sidebarOpen ? 'w-56' : 'w-0'
        } overflow-hidden`}
      >
        <div className="p-3 pb-2">
          <button
            onClick={handleNewResearch}
            className="group w-full flex items-center gap-2.5 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-3.5 py-2.5 text-[13px] font-medium text-gray-600 dark:text-gray-300 shadow-xs hover:shadow-md hover:border-gray-300 dark:hover:border-gray-600 transition-all duration-200"
          >
            <span className="flex h-5 w-5 items-center justify-center rounded-md bg-amber-600 dark:bg-amber-500 text-white text-xs transition-transform duration-200 group-hover:scale-110">
              +
            </span>
            New Research
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-2 pb-2">
          {history.length === 0 && (
            <div className="px-3 py-8 text-center text-xs text-gray-400 dark:text-gray-500">No research tasks yet</div>
          )}
          {history.map((task) => {
            const isActive = task.conversation_id === researchId
            return (
              <div
                key={task.conversation_id}
                onClick={() => navigate(`/research/${task.conversation_id}`)}
                className={`group relative flex items-start rounded-lg px-3 py-2.5 mb-0.5 cursor-pointer transition-all duration-150 ${
                  isActive
                    ? 'bg-white dark:bg-gray-800 shadow-xs ring-1 ring-gray-200/80 dark:ring-gray-700/60'
                    : 'hover:bg-white/60 dark:hover:bg-gray-800/40'
                }`}
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5">
                    <span className={`shrink-0 h-1.5 w-1.5 rounded-full ${statusColor(task.status)}`} />
                    <div
                      className={`text-[13px] font-medium truncate ${
                        isActive ? 'text-gray-900 dark:text-gray-100' : 'text-gray-600 dark:text-gray-400'
                      }`}
                    >
                      {task.title}
                    </div>
                  </div>
                  <div className="text-[11px] text-gray-400 dark:text-gray-500 mt-0.5 ml-3">
                    {formatRelativeDate(task.completed_at || task.created_at)}
                  </div>
                </div>
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    handleDiscard(task.conversation_id)
                  }}
                  className={`shrink-0 ml-1 mt-0.5 rounded p-0.5 transition-colors duration-150 ${
                    discardConfirm === task.conversation_id
                      ? 'text-red-500 bg-red-50 dark:bg-red-900/20'
                      : 'text-gray-300 dark:text-gray-600 opacity-0 group-hover:opacity-100 hover:text-red-400 dark:hover:text-red-400'
                  }`}
                  title={discardConfirm === task.conversation_id ? 'Click again to confirm' : 'Delete'}
                >
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            )
          })}
        </div>
      </div>

      {/* Main area */}
      <div className="flex flex-1 flex-col min-w-0 bg-white dark:bg-gray-900 overflow-hidden">
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
          <h2 className="text-[13px] font-semibold text-gray-700 dark:text-gray-300 truncate">
            {isRunning ? 'Research in progress...' : selectedTask ? selectedTask.title : 'Research'}
          </h2>
          {isRunning && (
            <div className="ml-auto flex items-center gap-1.5">
              <div className="flex gap-0.5">
                <span className="h-1 w-1 rounded-full bg-amber-500 animate-pulse" />
                <span className="h-1 w-1 rounded-full bg-amber-500 animate-pulse [animation-delay:0.2s]" />
                <span className="h-1 w-1 rounded-full bg-amber-500 animate-pulse [animation-delay:0.4s]" />
              </div>
              <span className="text-[11px] text-gray-400">Working</span>
            </div>
          )}
        </div>

        {/* Content area */}
        <div className="flex-1 overflow-y-auto">
          {/* State 1: Idle */}
          {isIdle && (
            <div className="flex h-full items-center justify-center p-8">
              <div className="text-center max-w-lg">
                <div className="mx-auto mb-6">
                  <img src="/research-octopus.png" alt="" className="h-80 mx-auto" />
                </div>
                <h3 className="text-[15px] font-semibold text-gray-800 dark:text-gray-200 mb-2">Deep Research</h3>
                <p className="text-[13px] text-gray-400 dark:text-gray-500 leading-relaxed mb-6 max-w-sm mx-auto">
                  Submit a question and Harbor Clerk will systematically search your documents to produce a
                  comprehensive report. This may take several minutes.
                </p>

                <div className="max-w-md mx-auto space-y-4">
                  <div className="rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-800/50 shadow-xs focus-within:shadow-md focus-within:border-gray-300 dark:focus-within:border-gray-600 transition-all duration-200">
                    <textarea
                      value={question}
                      onChange={(e) => setQuestion(e.target.value)}
                      onKeyDown={handleKeyDown}
                      placeholder="What would you like to research?"
                      rows={3}
                      className="w-full resize-none border-0 bg-transparent px-4 pt-3 pb-2 text-sm text-gray-800 dark:text-gray-200 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-hidden"
                      style={{ maxHeight: '160px' }}
                      onInput={(e) => {
                        const target = e.target as HTMLTextAreaElement
                        target.style.height = 'auto'
                        target.style.height = Math.min(target.scrollHeight, 160) + 'px'
                      }}
                    />
                  </div>

                  {/* Strategy toggle */}
                  <div className="flex items-center justify-center">
                    <div className="inline-flex rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-100 dark:bg-gray-800 p-0.5">
                      <button
                        onClick={() => setStrategy('search')}
                        className={`px-3 py-1.5 text-[12px] font-medium rounded-md transition-all duration-150 ${
                          strategy === 'search'
                            ? 'bg-white dark:bg-gray-700 text-gray-800 dark:text-gray-200 shadow-xs'
                            : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
                        }`}
                      >
                        Search-driven
                      </button>
                      <button
                        onClick={() => setStrategy('sweep')}
                        className={`px-3 py-1.5 text-[12px] font-medium rounded-md transition-all duration-150 ${
                          strategy === 'sweep'
                            ? 'bg-white dark:bg-gray-700 text-gray-800 dark:text-gray-200 shadow-xs'
                            : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
                        }`}
                      >
                        Systematic sweep
                      </button>
                    </div>
                  </div>

                  <button
                    onClick={handleStartResearch}
                    disabled={!question.trim()}
                    className="w-full rounded-lg bg-amber-600 dark:bg-amber-600 px-4 py-2.5 text-[13px] font-semibold text-white hover:bg-amber-700 dark:hover:bg-amber-500 disabled:opacity-40 disabled:hover:bg-amber-600 transition-colors duration-150"
                  >
                    Start Research
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* State 2: Running */}
          {isRunning && (
            <div className="flex flex-col items-center p-8">
              <div className="mb-4">
                <img src="/research-octopus.png" alt="" className="h-48 mx-auto" />
              </div>

              {progress && (
                <div className="w-full max-w-lg space-y-4">
                  {/* Round indicator */}
                  <div className="text-center">
                    <span className="text-[13px] font-semibold text-gray-700 dark:text-gray-300">
                      Round {progress.round} of {progress.maxRounds}
                    </span>
                    {progress.strategy === 'sweep' && progress.total != null && progress.reviewed != null && (
                      <div className="mt-2">
                        <div className="flex items-center justify-between text-[11px] text-gray-400 dark:text-gray-500 mb-1">
                          <span>Documents reviewed</span>
                          <span>
                            {progress.reviewed} of {progress.total}
                          </span>
                        </div>
                        <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-1.5">
                          <div
                            className="bg-amber-500 h-1.5 rounded-full transition-all duration-300"
                            style={{ width: `${Math.min(100, (progress.reviewed / progress.total) * 100)}%` }}
                          />
                        </div>
                      </div>
                    )}
                  </div>

                  {/* Synthesizing indicator */}
                  {isSynthesizing && (
                    <div className="flex items-center justify-center gap-2 py-2">
                      <svg className="h-4 w-4 text-amber-500 animate-spin" fill="none" viewBox="0 0 24 24">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                        <path
                          className="opacity-75"
                          fill="currentColor"
                          d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                        />
                      </svg>
                      <span className="text-[13px] font-medium text-amber-600 dark:text-amber-400">
                        Writing report...
                      </span>
                    </div>
                  )}

                  {/* Tool call log */}
                  {progress.toolCalls.length > 0 && (
                    <div className="rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-800/30 overflow-hidden">
                      <div className="px-3 py-2 text-[11px] font-medium text-gray-400 dark:text-gray-500 border-b border-gray-100 dark:border-gray-700/50">
                        Activity log
                      </div>
                      <div
                        ref={toolLogRef}
                        className="max-h-64 overflow-y-auto divide-y divide-gray-100 dark:divide-gray-700/30"
                      >
                        {progress.toolCalls.map((tc, i) => (
                          <ToolLogEntry key={i} tool={tc} isLast={i === progress.toolCalls.length - 1} />
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Cancel button */}
                  <div className="flex justify-center pt-2">
                    <button
                      onClick={cancelResearch}
                      className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-4 py-2 text-[13px] font-medium text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors duration-150"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}

              {!progress && (
                <div className="flex items-center gap-2 py-4">
                  <svg className="h-4 w-4 text-amber-500 animate-spin" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                  <span className="text-[13px] text-gray-400 dark:text-gray-500">Starting research...</span>
                </div>
              )}

              {error && (
                <div className="mt-4 rounded-lg bg-red-50 dark:bg-red-900/20 px-4 py-3 text-[13px] text-red-600 dark:text-red-400">
                  {error}
                </div>
              )}
            </div>
          )}

          {/* State 3: Completed */}
          {isViewingCompleted && selectedTask && (
            <div className="mx-auto px-6 py-6" style={{ maxWidth: 'min(100%, 72rem)' }}>
              {/* User question */}
              <div className="flex justify-end mb-6">
                <div className="max-w-[80%] rounded-xl rounded-tr-sm bg-gray-800 dark:bg-gray-700 text-gray-100 dark:text-gray-200 px-4 py-2.5 text-[13.5px] leading-relaxed">
                  {selectedTask.question}
                </div>
              </div>

              {/* Report */}
              {selectedTask.report && (
                <div className="rounded-xl bg-gray-50 dark:bg-gray-800/60 ring-1 ring-gray-100 dark:ring-gray-700/50 px-6 py-5">
                  <div className="prose-chat">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{selectedTask.report}</ReactMarkdown>
                  </div>
                </div>
              )}

              {/* Metadata */}
              <div className="mt-4 flex flex-wrap items-center gap-3 text-[11px] text-gray-400 dark:text-gray-500">
                {selectedTask.model_id && <span>Model: {selectedTask.model_id}</span>}
                <span className="capitalize">Strategy: {selectedTask.strategy}</span>
                <span>
                  Rounds: {selectedTask.current_round} / {selectedTask.max_rounds}
                </span>
                {selectedTask.completed_at && selectedTask.created_at && (
                  <span>
                    Time:{' '}
                    {formatElapsed(
                      Math.round(
                        (new Date(selectedTask.completed_at).getTime() - new Date(selectedTask.created_at).getTime()) /
                          1000,
                      ),
                    )}
                  </span>
                )}
              </div>
            </div>
          )}

          {/* State 4: Interrupted */}
          {isViewingInterrupted && selectedTask && (
            <div className="flex h-full items-center justify-center p-8">
              <div className="text-center max-w-lg">
                <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-amber-50 dark:bg-amber-900/20">
                  <svg
                    className="h-6 w-6 text-amber-500"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={1.5}
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"
                    />
                  </svg>
                </div>
                <h3 className="text-[15px] font-semibold text-gray-800 dark:text-gray-200 mb-2">
                  Research interrupted
                </h3>
                <p className="text-[13px] text-gray-400 dark:text-gray-500 mb-4">
                  This research task was interrupted before completion.
                </p>

                {/* Show question */}
                <div className="mb-6 rounded-lg bg-gray-50 dark:bg-gray-800/60 ring-1 ring-gray-100 dark:ring-gray-700/50 px-4 py-3 text-[13px] text-gray-700 dark:text-gray-300 text-left">
                  {selectedTask.question}
                </div>

                <div className="flex items-center justify-center gap-3">
                  <button
                    onClick={() => handleResume(selectedTask.conversation_id)}
                    className="rounded-lg bg-amber-600 px-4 py-2 text-[13px] font-semibold text-white hover:bg-amber-700 dark:hover:bg-amber-500 transition-colors duration-150"
                  >
                    Resume
                  </button>
                  <button
                    onClick={() => handleDiscard(selectedTask.conversation_id)}
                    className={`rounded-lg px-4 py-2 text-[13px] font-medium transition-colors duration-150 ${
                      discardConfirm === selectedTask.conversation_id
                        ? 'bg-red-600 text-white hover:bg-red-700'
                        : 'border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700'
                    }`}
                  >
                    {discardConfirm === selectedTask.conversation_id ? 'Confirm discard?' : 'Discard & Start New'}
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Error state (no task selected, just an error from a failed run) */}
          {!isRunning && !selectedTask && error && (
            <div className="flex h-full items-center justify-center p-8">
              <div className="text-center max-w-md">
                <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-red-50 dark:bg-red-900/20">
                  <svg
                    className="h-6 w-6 text-red-500"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={1.5}
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"
                    />
                  </svg>
                </div>
                <h3 className="text-[15px] font-semibold text-gray-800 dark:text-gray-200 mb-2">Research failed</h3>
                <p className="text-[13px] text-red-500 dark:text-red-400 mb-4">{error}</p>
                <button
                  onClick={handleNewResearch}
                  className="rounded-lg bg-amber-600 px-4 py-2 text-[13px] font-semibold text-white hover:bg-amber-700 transition-colors duration-150"
                >
                  Try Again
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/* ---- Tool log entry ---- */

function ToolLogEntry({ tool, isLast }: { tool: ToolCallEntry; isLast: boolean }) {
  const isDone = !!tool.summary

  return (
    <div className="flex items-start gap-2 px-3 py-2">
      <span
        className={`shrink-0 mt-0.5 ${
          !isDone && isLast
            ? 'text-blue-500 dark:text-blue-400 animate-pulse'
            : 'text-emerald-500 dark:text-emerald-400'
        }`}
      >
        {!isDone && isLast ? (
          <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"
            />
          </svg>
        ) : (
          <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
          </svg>
        )}
      </span>
      <div className="min-w-0 flex-1">
        <span className="text-[12px] font-medium text-gray-600 dark:text-gray-300">{tool.name}</span>
        {tool.summary && (
          <span className="ml-1.5 text-[11px] text-gray-400 dark:text-gray-500 truncate">{tool.summary}</span>
        )}
      </div>
    </div>
  )
}
