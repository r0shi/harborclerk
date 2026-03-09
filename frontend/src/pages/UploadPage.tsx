import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { get, getUploadSession, UploadSessionInfo } from '../api'
import { FileItem, useUploadSession } from '../hooks/useUploadSession'

// --- Types ---

interface FileWithFolder {
  file: File
  folderPath: string
}

// --- Constants ---

const SUPPORTED_EXTENSIONS = new Set([
  '.pdf',
  '.docx',
  '.doc',
  '.rtf',
  '.txt',
  '.md',
  '.odt',
  '.pages',
  '.xlsx',
  '.xls',
  '.ods',
  '.numbers',
  '.csv',
  '.pptx',
  '.ppt',
  '.odp',
  '.key',
  '.jpg',
  '.jpeg',
  '.png',
  '.tiff',
  '.tif',
  '.epub',
  '.html',
  '.htm',
  '.eml',
])

const AUTO_CONFIRM_THRESHOLD = 50

// --- Utilities ---

function isSupportedFile(name: string): boolean {
  const dot = name.lastIndexOf('.')
  if (dot === -1) return false
  return SUPPORTED_EXTENSIONS.has(name.slice(dot).toLowerCase())
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Drain all entries from a directory reader (spec requires repeated calls). */
async function readAllEntries(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  const all: FileSystemEntry[] = []
  let batch: FileSystemEntry[] = await new Promise((resolve, reject) => reader.readEntries(resolve, reject))
  while (batch.length > 0) {
    all.push(...batch)
    batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject))
  }
  return all
}

/** Recursively collect files from a FileSystemEntry tree, tracking folder paths. */
async function collectFiles(entry: FileSystemEntry, basePath = ''): Promise<FileWithFolder[]> {
  if (entry.isFile) {
    const fileEntry = entry as FileSystemFileEntry
    const file: File = await new Promise((resolve, reject) => fileEntry.file(resolve, reject))
    return isSupportedFile(file.name) ? [{ file, folderPath: basePath }] : []
  }
  if (entry.isDirectory) {
    const dirEntry = entry as FileSystemDirectoryEntry
    const folderPath = basePath ? `${basePath}/${entry.name}` : entry.name
    const entries = await readAllEntries(dirEntry.createReader())
    const nested = await Promise.all(entries.map((ent) => collectFiles(ent, folderPath)))
    return nested.flat()
  }
  return []
}

// --- Components ---

export default function UploadPage() {
  const hook = useUploadSession()
  const [dragOver, setDragOver] = useState(false)
  const [collectingFiles, setCollectingFiles] = useState(false)
  const [collectedFiles, setCollectedFiles] = useState<FileWithFolder[] | null>(null)
  const [cancelConfirm, setCancelConfirm] = useState(false)
  const [docsLoaded, setDocsLoaded] = useState(false)
  const [docCount, setDocCount] = useState(0)

  // Check existing doc count for auto-confirm heuristic
  useEffect(() => {
    get<{ items: { doc_id: string }[] }>('/api/docs', { limit: 0 })
      .then((data) => {
        setDocCount(data.items.length)
        setDocsLoaded(true)
      })
      .catch(() => setDocsLoaded(true))
  }, [])

  // Check for resumable session on mount
  const [resumeInfo, setResumeInfo] = useState<UploadSessionInfo | null>(null)
  const [resumeChecked, setResumeChecked] = useState(false)

  useEffect(() => {
    const sid = hook.sessionId
    if (!sid || hook.session) {
      setResumeChecked(true)
      return
    }
    getUploadSession(sid)
      .then((info) => {
        if (info.status === 'active') {
          setResumeInfo(info)
        } else {
          // Session is done, clear it
          hook.clearSession()
        }
      })
      .catch(() => {
        hook.clearSession()
      })
      .finally(() => setResumeChecked(true))
    // Only run on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleFilesSelected = useCallback(
    (filesWithFolders: FileWithFolder[]) => {
      if (filesWithFolders.length === 0) return

      const autoConfirm = filesWithFolders.length > AUTO_CONFIRM_THRESHOLD || (docsLoaded && docCount === 0)
      const inputs = filesWithFolders.map((f) => ({
        file: f.file,
        sourcePath: f.folderPath ? `${f.folderPath}/${f.file.name}` : f.file.name,
      }))

      // If review mode, save files for potential resume
      if (!autoConfirm) {
        setCollectedFiles(filesWithFolders)
      }

      hook.startSession(inputs, autoConfirm)
    },
    [hook, docsLoaded, docCount],
  )

  async function handleDrop(e: React.DragEvent) {
    e.preventDefault()
    setDragOver(false)

    const items = e.dataTransfer.items
    const entries: FileSystemEntry[] = []

    if (items) {
      for (let i = 0; i < items.length; i++) {
        const entry = items[i].webkitGetAsEntry?.()
        if (entry) entries.push(entry)
      }
    }

    setCollectingFiles(true)
    try {
      let files: FileWithFolder[] = []
      if (entries.length > 0) {
        const allNested = await Promise.all(entries.map((ent) => collectFiles(ent)))
        files = allNested.flat()
      } else if (e.dataTransfer.files.length > 0) {
        files = Array.from(e.dataTransfer.files)
          .filter((f) => f.size > 0 && isSupportedFile(f.name))
          .map((f) => ({ file: f, folderPath: '' }))
      }

      if (files.length === 0) {
        hook.clearSession()
        return
      }
      handleFilesSelected(files)
    } finally {
      setCollectingFiles(false)
    }
  }

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    if (e.target.files && e.target.files.length > 0) {
      const files = Array.from(e.target.files)
        .filter((f) => isSupportedFile(f.name))
        .map((f) => ({ file: f, folderPath: '' }))
      handleFilesSelected(files)
    }
  }

  function handleResume() {
    if (!collectedFiles && !resumeInfo) return
    // For resume, we need the user to re-select files (browser security prevents storing File refs)
    // Show them a message to re-drop the same folder
    setResumeInfo(null)
    if (collectedFiles) {
      const inputs = collectedFiles.map((f) => ({
        file: f.file,
        sourcePath: f.folderPath ? `${f.folderPath}/${f.file.name}` : f.file.name,
      }))
      hook.resumeSession(inputs)
    }
  }

  function handleStartFresh() {
    hook.clearSession()
    setResumeInfo(null)
    setCollectedFiles(null)
    setCancelConfirm(false)
  }

  async function handleCancel() {
    if (!cancelConfirm) {
      setCancelConfirm(true)
      return
    }
    await hook.cancel()
    setCancelConfirm(false)
  }

  const showDropZone = !hook.session || hook.session.status === 'completed' || hook.session.status === 'cancelled'

  if (!resumeChecked) return null

  return (
    <div>
      <h1 className="mb-4 text-xl font-bold">Upload Documents</h1>

      {hook.error && (
        <div className="mb-4 rounded-sm bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {hook.error}
        </div>
      )}

      {/* Resume banner */}
      {resumeInfo && !hook.session && (
        <div className="mb-4 rounded-xl border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/20 p-4 shadow-mac">
          <p className="text-sm font-medium text-blue-700 dark:text-blue-400">
            You have an in-progress upload ({resumeInfo.uploaded}/{resumeInfo.total_files} files).
          </p>
          <p className="mt-1 text-xs text-blue-600 dark:text-blue-500">
            Drop the same folder again to resume, or start fresh.
          </p>
          <div className="mt-3 flex gap-2">
            <button
              onClick={handleStartFresh}
              className="rounded-lg border border-blue-300 dark:border-blue-600 px-3 py-1 text-xs font-medium text-blue-700 dark:text-blue-400 hover:bg-blue-100 dark:hover:bg-blue-900/40"
            >
              Start Fresh
            </button>
          </div>
        </div>
      )}

      {/* Drop zone — shown when no active session */}
      {showDropZone && (
        <div
          onDragOver={(e) => {
            e.preventDefault()
            setDragOver(true)
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
          className={`flex flex-col items-center justify-center rounded-xl border-2 border-dashed p-12 transition-colors ${
            dragOver
              ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
              : 'border-gray-300/60 dark:border-gray-600/60 bg-white dark:bg-[#2c2c2e] shadow-mac'
          }`}
        >
          {collectingFiles ? (
            <p className="text-gray-500 dark:text-gray-400">Scanning files...</p>
          ) : (
            <>
              <p className="mb-2 text-gray-600 dark:text-gray-400">
                Drag and drop files or folders here, or click to browse
              </p>
              <p className="mb-4 text-xs text-gray-400">PDF, Office, text, images, eBooks, and more</p>
              <label className="cursor-pointer rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-xs hover:bg-blue-700">
                Choose Files
                <input
                  type="file"
                  multiple
                  onChange={handleFileChange}
                  className="hidden"
                  accept=".pdf,.docx,.doc,.rtf,.txt,.md,.odt,.pages,.xlsx,.xls,.ods,.numbers,.csv,.pptx,.ppt,.odp,.key,.jpg,.jpeg,.png,.tiff,.tif,.epub,.html,.htm,.eml"
                />
              </label>
            </>
          )}
        </div>
      )}

      {/* Active upload session */}
      {hook.session && hook.session.status !== 'completed' && hook.session.status !== 'cancelled' && (
        <SessionProgress
          session={hook.session}
          files={hook.files}
          progress={hook.progress}
          isUploading={hook.isUploading}
          isConfirming={hook.isConfirming}
          cancelConfirm={cancelConfirm}
          onCancel={handleCancel}
          onCancelDismiss={() => setCancelConfirm(false)}
          onConfirmAll={hook.confirmAll}
          onResume={handleResume}
        />
      )}

      {/* Completed session */}
      {hook.session?.status === 'completed' && (
        <div className="mt-6 space-y-3">
          <div className="rounded-xl border border-green-200 dark:border-green-800 bg-green-50 dark:bg-green-900/20 p-4 shadow-mac">
            <p className="text-sm font-medium text-green-700 dark:text-green-400">
              Upload complete — {hook.progress.uploaded} file{hook.progress.uploaded !== 1 ? 's' : ''} uploaded
              {hook.progress.failed > 0 && (
                <span className="text-red-600 dark:text-red-400"> ({hook.progress.failed} failed)</span>
              )}
            </p>
            <div className="mt-2 flex gap-2">
              <Link to="/docs" className="text-sm text-blue-600 dark:text-blue-400 hover:underline">
                View documents
              </Link>
              <button onClick={handleStartFresh} className="text-sm text-gray-500 dark:text-gray-400 hover:underline">
                Upload more
              </button>
            </div>
          </div>
          <FileList files={hook.files} />
        </div>
      )}

      {/* Cancelled */}
      {hook.session?.status === 'cancelled' && (
        <div className="mt-6">
          <div className="rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/30 p-4 shadow-mac">
            <p className="text-sm text-gray-600 dark:text-gray-400">Upload cancelled.</p>
            <button
              onClick={handleStartFresh}
              className="mt-2 text-sm text-blue-600 dark:text-blue-400 hover:underline"
            >
              Start new upload
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// --- Session Progress Panel ---

function SessionProgress({
  session,
  files,
  progress,
  isUploading,
  isConfirming,
  cancelConfirm,
  onCancel,
  onCancelDismiss,
  onConfirmAll,
}: {
  session: UploadSessionInfo
  files: FileItem[]
  progress: { total: number; uploaded: number; confirmed: number; failed: number; currentFiles: string[] }
  isUploading: boolean
  isConfirming: boolean
  cancelConfirm: boolean
  onCancel: () => void
  onCancelDismiss: () => void
  onConfirmAll: () => void
  onResume: () => void
}) {
  const pct = progress.total > 0 ? Math.round(((progress.uploaded + progress.failed) / progress.total) * 100) : 0

  return (
    <div className="mt-6 space-y-3">
      {/* Auto-confirm banner */}
      {session.auto_confirm && isUploading && (
        <div className="rounded-lg bg-blue-50 dark:bg-blue-900/20 px-3 py-2 text-xs text-blue-700 dark:text-blue-400">
          Bulk import mode — files are being processed immediately as they upload.
        </div>
      )}

      {/* Progress bar */}
      <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) overflow-hidden">
        <div className="px-4 py-3 flex items-center justify-between">
          <div>
            <h2 className="text-sm font-semibold">
              {isUploading
                ? 'Uploading...'
                : isConfirming
                  ? 'Confirming...'
                  : `${progress.uploaded} of ${progress.total} files uploaded`}
            </h2>
            <p className="text-xs text-gray-400 mt-0.5">
              {progress.uploaded} uploaded
              {progress.failed > 0 && <span className="text-red-500"> · {progress.failed} failed</span>}
              {session.auto_confirm && <span> · {progress.confirmed} processing</span>}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {/* Confirm button for review mode */}
            {!session.auto_confirm && !isUploading && progress.uploaded > 0 && session.status === 'active' && (
              <button
                onClick={onConfirmAll}
                disabled={isConfirming}
                className="rounded-lg bg-blue-600 px-3 py-1 text-xs font-medium text-white shadow-xs hover:bg-blue-700 disabled:opacity-50"
              >
                {isConfirming ? 'Confirming...' : `Confirm ${progress.uploaded - progress.confirmed}`}
              </button>
            )}
            {/* Cancel */}
            {isUploading && (
              <>
                {cancelConfirm ? (
                  <div className="flex items-center gap-1">
                    <span className="text-xs text-gray-500">Cancel upload?</span>
                    <button
                      onClick={onCancel}
                      className="rounded-lg bg-red-600 px-2 py-1 text-xs font-medium text-white hover:bg-red-700"
                    >
                      Yes
                    </button>
                    <button
                      onClick={onCancelDismiss}
                      className="rounded-lg border border-gray-200 dark:border-gray-600 px-2 py-1 text-xs text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-700/50"
                    >
                      No
                    </button>
                  </div>
                ) : (
                  <button
                    onClick={onCancel}
                    className="rounded-lg border border-gray-200 dark:border-gray-600 px-3 py-1 text-xs font-medium text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-700/50"
                  >
                    Cancel
                  </button>
                )}
              </>
            )}
          </div>
        </div>

        {/* Progress bar */}
        <div className="h-1 bg-gray-100 dark:bg-gray-700">
          <div className="h-full bg-blue-600 transition-all duration-300" style={{ width: `${pct}%` }} />
        </div>

        {/* Currently uploading files */}
        {progress.currentFiles.length > 0 && (
          <div className="px-4 py-2 border-t border-gray-50 dark:border-gray-700/30">
            <p className="text-xs text-gray-400">Uploading: {progress.currentFiles.join(', ')}</p>
          </div>
        )}
      </div>

      {/* File list */}
      <FileList files={files} />
    </div>
  )
}

// --- File List ---

function FileList({ files }: { files: FileItem[] }) {
  const [showAll, setShowAll] = useState(false)

  const failed = useMemo(() => files.filter((f) => f.status === 'error'), [files])
  const duplicates = useMemo(() => files.filter((f) => f.status === 'duplicate'), [files])
  const done = useMemo(() => files.filter((f) => f.status === 'done'), [files])
  const pending = useMemo(() => files.filter((f) => f.status === 'pending' || f.status === 'uploading'), [files])

  // Show compact summary unless expanded
  const displayFiles = showAll ? files : files.slice(0, 20)
  const hasMore = files.length > 20 && !showAll

  return (
    <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) overflow-hidden">
      {/* Summary row */}
      <div className="px-4 py-2 border-b border-gray-50 dark:border-gray-700/30 flex items-center gap-3 text-xs text-gray-500 dark:text-gray-400">
        {done.length > 0 && (
          <span className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full bg-green-500" />
            {done.length} done
          </span>
        )}
        {duplicates.length > 0 && (
          <span className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full bg-amber-500" />
            {duplicates.length} duplicate{duplicates.length !== 1 ? 's' : ''}
          </span>
        )}
        {pending.length > 0 && (
          <span className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full bg-gray-400" />
            {pending.length} pending
          </span>
        )}
        {failed.length > 0 && (
          <span className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full bg-red-500" />
            {failed.length} failed
          </span>
        )}
      </div>

      {/* Failed files first */}
      {failed.length > 0 && (
        <div className="border-b border-red-100 dark:border-red-900/30">
          {failed.map((f) => (
            <FileItemRow key={f.id} item={f} />
          ))}
        </div>
      )}

      {/* All files */}
      <div className="divide-y divide-gray-50 dark:divide-gray-700/30">
        {displayFiles
          .filter((f) => f.status !== 'error')
          .map((f) => (
            <FileItemRow key={f.id} item={f} />
          ))}
      </div>

      {hasMore && (
        <div className="px-4 py-2 border-t border-gray-50 dark:border-gray-700/30">
          <button onClick={() => setShowAll(true)} className="text-xs text-blue-600 dark:text-blue-400 hover:underline">
            Show all {files.length} files
          </button>
        </div>
      )}
    </div>
  )
}

// --- Single file row ---

const STATUS_STYLES: Record<string, { dot: string; label: string }> = {
  pending: { dot: 'bg-gray-400', label: 'Pending' },
  uploading: { dot: 'bg-blue-500 animate-pulse', label: 'Uploading' },
  done: { dot: 'bg-green-500', label: 'Done' },
  error: { dot: 'bg-red-500', label: 'Failed' },
  duplicate: { dot: 'bg-amber-500', label: 'Duplicate' },
}

function FileItemRow({ item }: { item: FileItem }) {
  const style = STATUS_STYLES[item.status] || STATUS_STYLES.pending

  return (
    <div className="flex items-center gap-3 px-4 py-2 hover:bg-gray-50/50 dark:hover:bg-gray-800/20">
      <span className={`inline-block h-2 w-2 rounded-full shrink-0 ${style.dot}`} />
      <div className="min-w-0 flex-1">
        <span className="text-sm truncate block">{item.file.name}</span>
        {item.sourcePath && item.sourcePath !== item.file.name && (
          <span className="text-xs text-gray-400 truncate block">{item.sourcePath}</span>
        )}
      </div>
      <span className="text-xs text-gray-400 shrink-0">{formatSize(item.file.size)}</span>
      <span className="text-xs text-gray-500 dark:text-gray-400 shrink-0 w-16 text-right">{style.label}</span>
      {item.status === 'error' && item.error && (
        <span className="text-xs text-red-500 shrink-0 max-w-[200px] truncate" title={item.error}>
          {item.error}
        </span>
      )}
      {item.status === 'done' && item.result?.doc_id && (
        <Link
          to={`/docs/${item.result.doc_id}`}
          className="text-xs text-blue-600 dark:text-blue-400 hover:underline shrink-0"
        >
          View
        </Link>
      )}
      {item.status === 'duplicate' && item.result?.duplicate_doc_id && (
        <Link
          to={`/docs/${item.result.duplicate_doc_id}`}
          className="text-xs text-amber-600 dark:text-amber-400 hover:underline shrink-0"
        >
          Existing
        </Link>
      )}
    </div>
  )
}
