import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { get, post, postForm } from '../api'

// --- Types ---

interface UploadFileResult {
  upload_id: string
  filename: string
  size_bytes: number
  mime_type?: string
  status: 'pending_confirmation' | 'duplicate' | 'skipped'
  duplicate_doc_id?: string
  duplicate_version_id?: string
}

interface DocSummary {
  doc_id: string
  title: string
  canonical_filename: string | null
}

interface ConfirmResult {
  doc_id: string
  version_id: string
  status: string
}

interface BatchConfirmResultItem {
  upload_id: string
  doc_id?: string
  version_id?: string
  status: string
  error?: string
}

interface FileWithFolder {
  file: File
  folderPath: string
}

interface MatchCandidate {
  doc: DocSummary
  score: number
  reason: string
}

interface PendingFile {
  uploadId: string
  filename: string
  sizeBytes: number
  folderPath: string
  matchCandidates: MatchCandidate[]
  action: 'new_document' | 'new_version'
  selectedDocId?: string
}

// --- Constants ---

const SUPPORTED_EXTENSIONS = new Set([
  // Documents
  '.pdf', '.docx', '.doc', '.rtf', '.txt', '.md',
  '.odt', '.pages',
  // Spreadsheets
  '.xlsx', '.xls', '.ods', '.numbers', '.csv',
  // Presentations
  '.pptx', '.ppt', '.odp', '.key',
  // Images (OCR)
  '.jpg', '.jpeg', '.png', '.tiff', '.tif',
  // eBooks
  '.epub',
  // Web
  '.html', '.htm',
  // Email
  '.eml',
])

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

function findMatchCandidates(
  filename: string,
  docs: DocSummary[],
): MatchCandidate[] {
  const stem = filename.replace(/\.[^.]+$/, '').toLowerCase()
  const candidates: MatchCandidate[] = []

  for (const doc of docs) {
    const docFilename = (doc.canonical_filename || '').toLowerCase()
    const docStem = docFilename.replace(/\.[^.]+$/, '')

    if (filename.toLowerCase() === docFilename) {
      candidates.push({ doc, score: 1.0, reason: 'Exact filename match' })
    } else if (stem === docStem) {
      candidates.push({ doc, score: 0.8, reason: 'Same name, different format' })
    } else if (
      stem.length > 3 &&
      (docStem.includes(stem) || stem.includes(docStem))
    ) {
      candidates.push({ doc, score: 0.5, reason: 'Similar name' })
    }
  }

  return candidates.sort((a, b) => b.score - a.score)
}

/** Drain all entries from a directory reader (spec requires repeated calls). */
async function readAllEntries(
  reader: FileSystemDirectoryReader,
): Promise<FileSystemEntry[]> {
  const all: FileSystemEntry[] = []
  let batch: FileSystemEntry[] = await new Promise((resolve, reject) =>
    reader.readEntries(resolve, reject),
  )
  while (batch.length > 0) {
    all.push(...batch)
    batch = await new Promise((resolve, reject) =>
      reader.readEntries(resolve, reject),
    )
  }
  return all
}

/** Recursively collect files from a FileSystemEntry tree, tracking folder paths. */
async function collectFiles(
  entry: FileSystemEntry,
  basePath = '',
): Promise<FileWithFolder[]> {
  if (entry.isFile) {
    const fileEntry = entry as FileSystemFileEntry
    const file: File = await new Promise((resolve, reject) =>
      fileEntry.file(resolve, reject),
    )
    return isSupportedFile(file.name) ? [{ file, folderPath: basePath }] : []
  }
  if (entry.isDirectory) {
    const dirEntry = entry as FileSystemDirectoryEntry
    const folderPath = basePath ? `${basePath}/${entry.name}` : entry.name
    const entries = await readAllEntries(dirEntry.createReader())
    const nested = await Promise.all(
      entries.map((ent) => collectFiles(ent, folderPath)),
    )
    return nested.flat()
  }
  return []
}

// --- Components ---

export default function UploadPage() {
  const [results, setResults] = useState<UploadFileResult[]>([])
  const [uploading, setUploading] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState('')
  const [dragOver, setDragOver] = useState(false)
  const [confirmed, setConfirmed] = useState<Map<string, ConfirmResult>>(
    new Map(),
  )
  const [docs, setDocs] = useState<DocSummary[]>([])
  const [docsLoaded, setDocsLoaded] = useState(false)
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([])

  useEffect(() => {
    get<DocSummary[]>('/api/docs')
      .then((d) => {
        setDocs(d)
        setDocsLoaded(true)
      })
      .catch(() => setDocsLoaded(true))
  }, [])

  const unconfirmedPending = useMemo(
    () => pendingFiles.filter((p) => !confirmed.has(p.uploadId)),
    [pendingFiles, confirmed],
  )

  const hasMatchSuggestions = useMemo(
    () => unconfirmedPending.some((p) => p.matchCandidates.length > 0),
    [unconfirmedPending],
  )

  async function batchConfirm(items: PendingFile[]) {
    setConfirming(true)
    setError('')
    try {
      const body = {
        items: items.map((p) => ({
          upload_id: p.uploadId,
          action: p.action,
          existing_doc_id:
            p.action === 'new_version' ? p.selectedDocId : undefined,
        })),
      }
      const data = await post<{ results: BatchConfirmResultItem[] }>(
        '/api/uploads/confirm-batch',
        body,
      )
      setConfirmed((prev) => {
        const next = new Map(prev)
        for (const r of data.results) {
          if (r.status !== 'error' && r.doc_id && r.version_id) {
            next.set(r.upload_id, {
              doc_id: r.doc_id,
              version_id: r.version_id,
              status: r.status,
            })
          }
        }
        return next
      })
      const errors = data.results.filter((r) => r.status === 'error')
      if (errors.length > 0) {
        setError(errors.map((e) => e.error).join('; '))
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Confirm failed')
    } finally {
      setConfirming(false)
    }
  }

  const uploadFiles = useCallback(
    async (filesWithFolders: FileWithFolder[]) => {
      setError('')
      setUploading(true)
      setResults([])
      setConfirmed(new Map())
      setPendingFiles([])

      try {
        const formData = new FormData()
        for (const { file } of filesWithFolders) {
          formData.append('files', file)
        }
        const data = await postForm<{ files: UploadFileResult[] }>(
          '/api/uploads',
          formData,
        )
        setResults(data.files)

        // Build pending files with folder paths and match candidates
        const pending: PendingFile[] = []
        data.files.forEach((r, i) => {
          if (r.status !== 'pending_confirmation') return
          const folderPath = filesWithFolders[i]?.folderPath || ''
          const matches = findMatchCandidates(r.filename, docs)
          const bestMatch =
            matches.length > 0 && matches[0].score >= 0.8 ? matches[0] : null
          pending.push({
            uploadId: r.upload_id,
            filename: r.filename,
            sizeBytes: r.size_bytes,
            folderPath,
            matchCandidates: matches,
            action: bestMatch ? 'new_version' : 'new_document',
            selectedDocId: bestMatch ? bestMatch.doc.doc_id : undefined,
          })
        })
        setPendingFiles(pending)
        setUploading(false)

        // Auto-confirm if no existing docs
        if (docsLoaded && docs.length === 0 && pending.length > 0) {
          await batchConfirm(pending)
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Upload failed')
        setUploading(false)
      }
    },
    [docs, docsLoaded],
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

    if (entries.length > 0) {
      const allNested = await Promise.all(
        entries.map((ent) => collectFiles(ent)),
      )
      const files = allNested.flat()
      if (files.length === 0) {
        setError(
          'No supported files found. Supported types: PDF, DOCX, DOC, RTF, TXT, MD, ODT, XLSX, XLS, CSV, PPTX, PPT, JPEG, PNG, TIFF, EPUB, HTML, EML',
        )
        return
      }
      uploadFiles(files)
      return
    }

    if (e.dataTransfer.files.length > 0) {
      const supported = Array.from(e.dataTransfer.files).filter((f) =>
        isSupportedFile(f.name),
      )
      if (supported.length === 0) {
        setError(
          'No supported files found. Supported types: PDF, DOCX, DOC, RTF, TXT, MD, ODT, XLSX, XLS, CSV, PPTX, PPT, JPEG, PNG, TIFF, EPUB, HTML, EML',
        )
        return
      }
      uploadFiles(supported.map((f) => ({ file: f, folderPath: '' })))
    }
  }

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    if (e.target.files && e.target.files.length > 0) {
      uploadFiles(
        Array.from(e.target.files).map((f) => ({ file: f, folderPath: '' })),
      )
    }
  }

  function updatePendingAction(uploadId: string, value: string) {
    setPendingFiles((prev) =>
      prev.map((p) => {
        if (p.uploadId !== uploadId) return p
        if (value === 'new') {
          return {
            ...p,
            action: 'new_document' as const,
            selectedDocId: undefined,
          }
        }
        const docId = value.replace('version:', '')
        return {
          ...p,
          action: 'new_version' as const,
          selectedDocId: docId,
        }
      }),
    )
  }

  function setAllToNew() {
    setPendingFiles((prev) =>
      prev.map((p) => ({
        ...p,
        action: 'new_document' as const,
        selectedDocId: undefined,
      })),
    )
  }

  function acceptAllSuggestions() {
    setPendingFiles((prev) =>
      prev.map((p) => {
        if (p.matchCandidates.length > 0) {
          return {
            ...p,
            action: 'new_version' as const,
            selectedDocId: p.matchCandidates[0].doc.doc_id,
          }
        }
        return p
      }),
    )
  }

  function setFolderToNew(folderPath: string) {
    setPendingFiles((prev) =>
      prev.map((p) =>
        p.folderPath === folderPath
          ? {
              ...p,
              action: 'new_document' as const,
              selectedDocId: undefined,
            }
          : p,
      ),
    )
  }

  // Group unconfirmed pending files by folder
  const folderGroups = useMemo(() => {
    const groups: [string, PendingFile[]][] = []
    const map = new Map<string, PendingFile[]>()
    for (const p of unconfirmedPending) {
      const key = p.folderPath
      if (!map.has(key)) {
        const arr: PendingFile[] = []
        map.set(key, arr)
        groups.push([key, arr])
      }
      map.get(key)!.push(p)
    }
    return groups
  }, [unconfirmedPending])

  const confirmedFiles = pendingFiles.filter((p) => confirmed.has(p.uploadId))
  const nonPendingResults = results.filter(
    (r) => r.status === 'duplicate' || r.status === 'skipped',
  )

  return (
    <div>
      <h1 className="mb-4 text-xl font-bold">Upload Documents</h1>

      {error && (
        <div className="mb-4 rounded bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      {/* Drop zone */}
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
        {uploading || confirming ? (
          <p className="text-gray-500 dark:text-gray-400">
            {uploading ? 'Uploading...' : 'Starting processing...'}
          </p>
        ) : (
          <>
            <p className="mb-2 text-gray-600 dark:text-gray-400">
              Drag and drop files or folders here, or click to browse
            </p>
            <p className="mb-4 text-xs text-gray-400">
              PDF, Office, text, images, eBooks, and more
            </p>
            <label className="cursor-pointer rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-blue-700">
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

      {/* Results */}
      {results.length > 0 && (
        <div className="mt-6 space-y-3">
          {/* Non-pending results (duplicates, skipped) */}
          {nonPendingResults.map((r) => (
            <StatusCard key={r.upload_id} result={r} />
          ))}

          {/* Confirmed files */}
          {confirmedFiles.map((p) => {
            const c = confirmed.get(p.uploadId)!
            return (
              <div
                key={p.uploadId}
                className="rounded-xl border border-green-200 dark:border-green-800 bg-green-50 dark:bg-green-900/20 p-4 shadow-mac"
              >
                <p className="text-sm font-medium text-green-700 dark:text-green-400">
                  {p.filename} — Processing started
                </p>
                <Link
                  to={`/docs/${c.doc_id}`}
                  className="text-sm text-blue-600 dark:text-blue-400 hover:underline"
                >
                  View document
                </Link>
              </div>
            )
          })}

          {/* Batch confirm UI */}
          {unconfirmedPending.length > 0 && (
            <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac overflow-hidden">
              {/* Header */}
              <div className="border-b border-gray-100 dark:border-gray-700/50 px-4 py-3 flex items-center justify-between flex-wrap gap-2">
                <h2 className="text-sm font-semibold">
                  {unconfirmedPending.length} file
                  {unconfirmedPending.length !== 1 ? 's' : ''} ready to import
                </h2>
                <div className="flex gap-2">
                  {hasMatchSuggestions && (
                    <button
                      onClick={acceptAllSuggestions}
                      className="rounded-lg border border-amber-300 dark:border-amber-600 px-3 py-1 text-xs font-medium text-amber-700 dark:text-amber-400 hover:bg-amber-50 dark:hover:bg-amber-900/20"
                    >
                      Accept All Suggestions
                    </button>
                  )}
                  <button
                    onClick={setAllToNew}
                    className="rounded-lg border border-gray-200 dark:border-gray-600 px-3 py-1 text-xs font-medium text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-700/50"
                  >
                    All as New Documents
                  </button>
                </div>
              </div>

              {/* File groups */}
              <div className="divide-y divide-gray-50 dark:divide-gray-700/30">
                {folderGroups.map(([folder, files]) => (
                  <div key={folder || '__root__'}>
                    {(folder || folderGroups.length > 1) && (
                      <div className="flex items-center justify-between px-4 py-2 bg-gray-50/50 dark:bg-gray-800/30">
                        <span className="text-xs font-medium text-gray-500 dark:text-gray-400">
                          {folder || 'Files'}
                        </span>
                        {files.length > 1 && (
                          <button
                            onClick={() => setFolderToNew(folder)}
                            className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                          >
                            All as new
                          </button>
                        )}
                      </div>
                    )}
                    {files.map((p) => (
                      <FileRow
                        key={p.uploadId}
                        pending={p}
                        docs={docs}
                        onChange={updatePendingAction}
                      />
                    ))}
                  </div>
                ))}
              </div>

              {/* Confirm button */}
              <div className="border-t border-gray-100 dark:border-gray-700/50 px-4 py-3 flex justify-end">
                <button
                  onClick={() => batchConfirm(unconfirmedPending)}
                  disabled={confirming}
                  className="rounded-lg bg-blue-600 px-4 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-blue-700 disabled:opacity-50"
                >
                  {confirming
                    ? 'Confirming...'
                    : `Confirm ${unconfirmedPending.length} file${unconfirmedPending.length !== 1 ? 's' : ''}`}
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function FileRow({
  pending,
  docs,
  onChange,
}: {
  pending: PendingFile
  docs: DocSummary[]
  onChange: (uploadId: string, value: string) => void
}) {
  const otherDocs = docs.filter(
    (d) => !pending.matchCandidates.some((m) => m.doc.doc_id === d.doc_id),
  )
  const matchReason =
    pending.action === 'new_version'
      ? pending.matchCandidates.find(
          (m) => m.doc.doc_id === pending.selectedDocId,
        )?.reason
      : null

  return (
    <div className="flex items-center gap-3 px-4 py-2.5 hover:bg-gray-50/50 dark:hover:bg-gray-800/20">
      <div className="min-w-0 flex-1">
        <span className="text-sm truncate block">{pending.filename}</span>
        <span className="text-xs text-gray-400">
          {formatSize(pending.sizeBytes)}
        </span>
      </div>
      {matchReason && (
        <span className="text-xs text-amber-600 dark:text-amber-400 shrink-0 hidden sm:inline">
          {matchReason}
        </span>
      )}
      <select
        value={
          pending.action === 'new_document'
            ? 'new'
            : `version:${pending.selectedDocId}`
        }
        onChange={(e) => onChange(pending.uploadId, e.target.value)}
        className="shrink-0 rounded-lg border-0 bg-gray-100 dark:bg-gray-700/50 px-2 py-1 text-xs focus:ring-2 focus:ring-blue-500/30"
      >
        <option value="new">New document</option>
        {pending.matchCandidates.length > 0 && (
          <optgroup label="Suggested">
            {pending.matchCandidates.map((m) => (
              <option key={m.doc.doc_id} value={`version:${m.doc.doc_id}`}>
                Update &ldquo;{m.doc.title}&rdquo;
              </option>
            ))}
          </optgroup>
        )}
        {otherDocs.length > 0 && (
          <optgroup label="Other documents">
            {otherDocs.map((d) => (
              <option key={d.doc_id} value={`version:${d.doc_id}`}>
                {d.title}
              </option>
            ))}
          </optgroup>
        )}
      </select>
    </div>
  )
}

function StatusCard({ result }: { result: UploadFileResult }) {
  if (result.status === 'duplicate') {
    return (
      <div className="rounded-xl border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20 p-4 shadow-mac">
        <p className="text-sm font-medium text-amber-700 dark:text-amber-400">
          {result.filename} — Duplicate
        </p>
        {result.duplicate_doc_id && (
          <Link
            to={`/docs/${result.duplicate_doc_id}`}
            className="text-sm text-blue-600 dark:text-blue-400 hover:underline"
          >
            View existing document
          </Link>
        )}
      </div>
    )
  }

  return (
    <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac p-4">
      <p className="text-sm text-gray-500 dark:text-gray-400">
        {result.filename} — Skipped (unsupported type)
      </p>
    </div>
  )
}
