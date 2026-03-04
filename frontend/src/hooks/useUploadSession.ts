import { useCallback, useEffect, useRef, useState } from 'react'
import {
  cancelSession,
  confirmSession,
  createUploadSession,
  getResumeInfo,
  getUploadSession,
  SessionFileResult,
  uploadFileToSession,
  UploadSessionInfo,
} from '../api'

export type FileItemStatus = 'pending' | 'uploading' | 'done' | 'error' | 'duplicate'

export interface FileItem {
  id: string // unique key (source_path or index-based)
  file: File
  sourcePath: string
  status: FileItemStatus
  error?: string
  result?: SessionFileResult
  retries: number
}

export interface UploadProgress {
  total: number
  uploaded: number
  confirmed: number
  failed: number
  currentFiles: string[]
}

export interface UseUploadSessionReturn {
  /** Start a new upload session with the given files */
  startSession: (files: Array<{ file: File; sourcePath: string }>, autoConfirm: boolean) => Promise<void>
  /** Resume an existing session */
  resumeSession: (files: Array<{ file: File; sourcePath: string }>) => Promise<void>
  /** Confirm all pending files (review mode) */
  confirmAll: () => Promise<void>
  /** Cancel the current session */
  cancel: () => Promise<void>
  /** Current session info */
  session: UploadSessionInfo | null
  /** Per-file items */
  files: FileItem[]
  /** Aggregated progress */
  progress: UploadProgress
  /** Whether uploads are actively running */
  isUploading: boolean
  /** Whether confirmation is in progress */
  isConfirming: boolean
  /** Global error message */
  error: string | null
  /** Active session ID (for sessionStorage persistence) */
  sessionId: string | null
  /** Clear the session state */
  clearSession: () => void
}

const SESSION_STORAGE_KEY = 'hc_upload_session_id'
const CONCURRENCY = 3
const MAX_RETRIES = 2

export function useUploadSession(): UseUploadSessionReturn {
  const [session, setSession] = useState<UploadSessionInfo | null>(null)
  const [files, setFiles] = useState<FileItem[]>([])
  const [isUploading, setIsUploading] = useState(false)
  const [isConfirming, setIsConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sessionId, setSessionId] = useState<string | null>(() => sessionStorage.getItem(SESSION_STORAGE_KEY))

  const abortRef = useRef<AbortController | null>(null)
  const filesRef = useRef<FileItem[]>([])
  filesRef.current = files

  // Persist session ID to sessionStorage
  useEffect(() => {
    if (sessionId) {
      sessionStorage.setItem(SESSION_STORAGE_KEY, sessionId)
    } else {
      sessionStorage.removeItem(SESSION_STORAGE_KEY)
    }
  }, [sessionId])

  const computeProgress = useCallback((): UploadProgress => {
    const total = files.length
    const uploaded = files.filter((f) => f.status === 'done' || f.status === 'duplicate').length
    const failed = files.filter((f) => f.status === 'error').length
    const currentFiles = files.filter((f) => f.status === 'uploading').map((f) => f.file.name)
    return { total, uploaded, confirmed: session?.confirmed ?? 0, failed, currentFiles }
  }, [files, session])

  const updateFile = useCallback((id: string, updates: Partial<FileItem>) => {
    setFiles((prev) => prev.map((f) => (f.id === id ? { ...f, ...updates } : f)))
  }, [])

  /** Run the concurrency pool over pending files */
  const runPool = useCallback(
    async (items: FileItem[], sid: string, abort: AbortController) => {
      // Build a queue of items to upload
      const queue = items.filter((f) => f.status === 'pending').map((f) => f.id)
      let activeCount = 0
      let queueIndex = 0

      await new Promise<void>((resolve) => {
        const tryNext = () => {
          if (abort.signal.aborted) {
            if (activeCount === 0) resolve()
            return
          }

          while (activeCount < CONCURRENCY && queueIndex < queue.length) {
            const fileId = queue[queueIndex++]
            const item = filesRef.current.find((f) => f.id === fileId)
            if (!item || item.status !== 'pending') {
              continue
            }

            activeCount++
            updateFile(fileId, { status: 'uploading' })

            uploadFileToSession(sid, item.file, item.sourcePath, abort.signal)
              .then((result) => {
                const newStatus: FileItemStatus = result.status === 'duplicate' ? 'duplicate' : 'done'
                updateFile(fileId, { status: newStatus, result })
              })
              .catch((err) => {
                if (abort.signal.aborted) return
                const currentItem = filesRef.current.find((f) => f.id === fileId)
                const retries = currentItem?.retries ?? 0
                if (retries < MAX_RETRIES) {
                  // Re-queue for retry
                  updateFile(fileId, { status: 'pending', retries: retries + 1 })
                  queue.push(fileId)
                } else {
                  updateFile(fileId, {
                    status: 'error',
                    error: err instanceof Error ? err.message : 'Upload failed',
                  })
                }
              })
              .finally(() => {
                activeCount--
                tryNext()
                // Check if all done
                if (activeCount === 0 && queueIndex >= queue.length) {
                  resolve()
                }
              })
          }

          // If queue is exhausted and nothing active, resolve
          if (activeCount === 0 && queueIndex >= queue.length) {
            resolve()
          }
        }

        tryNext()
      })
    },
    [updateFile],
  )

  const startSession = useCallback(
    async (fileInputs: Array<{ file: File; sourcePath: string }>, autoConfirm: boolean) => {
      setError(null)

      try {
        const sessionInfo = await createUploadSession({
          total_files: fileInputs.length,
          auto_confirm: autoConfirm,
        })
        setSession(sessionInfo)
        setSessionId(sessionInfo.session_id)

        const items: FileItem[] = fileInputs.map((f, i) => ({
          id: f.sourcePath || `file-${i}`,
          file: f.file,
          sourcePath: f.sourcePath,
          status: 'pending' as FileItemStatus,
          retries: 0,
        }))
        setFiles(items)
        filesRef.current = items // sync ref immediately so runPool can find items before re-render
        setIsUploading(true)

        const abort = new AbortController()
        abortRef.current = abort

        await runPool(items, sessionInfo.session_id, abort)

        // Refresh session info
        const updated = await getUploadSession(sessionInfo.session_id)
        setSession(updated)
        setIsUploading(false)

        // If auto-confirm, mark session complete
        if (autoConfirm) {
          setSession((prev) => (prev ? { ...prev, status: 'completed' } : prev))
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to start upload session')
        setIsUploading(false)
      }
    },
    [runPool],
  )

  const resumeSession = useCallback(
    async (fileInputs: Array<{ file: File; sourcePath: string }>) => {
      if (!sessionId) {
        setError('No session to resume')
        return
      }
      setError(null)

      try {
        const sessionInfo = await getUploadSession(sessionId)
        setSession(sessionInfo)

        if (sessionInfo.status !== 'active') {
          setError(`Session is ${sessionInfo.status}, cannot resume`)
          return
        }

        // Get completed paths
        const { completed_paths } = await getResumeInfo(sessionId)
        const completedSet = new Set(completed_paths)

        const items: FileItem[] = fileInputs.map((f, i) => ({
          id: f.sourcePath || `file-${i}`,
          file: f.file,
          sourcePath: f.sourcePath,
          status: completedSet.has(f.sourcePath) ? ('done' as FileItemStatus) : ('pending' as FileItemStatus),
          retries: 0,
        }))
        setFiles(items)
        filesRef.current = items // sync ref immediately so runPool can find items before re-render

        const pending = items.filter((f) => f.status === 'pending')
        if (pending.length === 0) {
          const updated = await getUploadSession(sessionId)
          setSession(updated)
          return
        }

        setIsUploading(true)
        const abort = new AbortController()
        abortRef.current = abort

        await runPool(items, sessionId, abort)

        const updated = await getUploadSession(sessionId)
        setSession(updated)
        setIsUploading(false)
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to resume session')
        setIsUploading(false)
      }
    },
    [sessionId, runPool],
  )

  const confirmAll = useCallback(async () => {
    if (!sessionId) return
    setIsConfirming(true)
    setError(null)

    try {
      const result = await confirmSession(sessionId)
      const errors = result.results.filter((r) => r.status === 'error')
      if (errors.length > 0) {
        setError(`${errors.length} file(s) failed to confirm`)
      }

      // Update file items with confirmation results
      setFiles((prev) =>
        prev.map((f) => {
          const match = result.results.find((r) => r.upload_id === f.result?.upload_id)
          if (match && match.status !== 'error') {
            return {
              ...f,
              result: f.result
                ? { ...f.result, doc_id: match.doc_id, version_id: match.version_id, status: match.status }
                : f.result,
            }
          }
          return f
        }),
      )

      const updated = await getUploadSession(sessionId)
      setSession(updated)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Confirmation failed')
    } finally {
      setIsConfirming(false)
    }
  }, [sessionId])

  const cancel = useCallback(async () => {
    if (abortRef.current) {
      abortRef.current.abort()
    }
    if (sessionId) {
      try {
        await cancelSession(sessionId)
      } catch {
        // Best effort
      }
    }
    setIsUploading(false)
    setSession((prev) => (prev ? { ...prev, status: 'cancelled' } : prev))
  }, [sessionId])

  const clearSession = useCallback(() => {
    setSession(null)
    setSessionId(null)
    setFiles([])
    setError(null)
    setIsUploading(false)
    setIsConfirming(false)
    abortRef.current = null
  }, [])

  return {
    startSession,
    resumeSession,
    confirmAll,
    cancel,
    session,
    files,
    progress: computeProgress(),
    isUploading,
    isConfirming,
    error,
    sessionId,
    clearSession,
  }
}
