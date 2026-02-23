import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { get, post, postForm } from '../api'

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
}

interface ConfirmResult {
  doc_id: string
  version_id: string
  status: string
}

const SUPPORTED_EXTENSIONS = new Set([
  '.pdf',
  '.docx',
  '.rtf',
  '.txt',
  '.jpg',
  '.jpeg',
])

function isSupportedFile(name: string): boolean {
  const dot = name.lastIndexOf('.')
  if (dot === -1) return false
  return SUPPORTED_EXTENSIONS.has(name.slice(dot).toLowerCase())
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

/** Recursively collect files from a FileSystemEntry tree, filtering by extension. */
async function collectFiles(entry: FileSystemEntry): Promise<File[]> {
  if (entry.isFile) {
    const fileEntry = entry as FileSystemFileEntry
    const file: File = await new Promise((resolve, reject) =>
      fileEntry.file(resolve, reject),
    )
    return isSupportedFile(file.name) ? [file] : []
  }
  if (entry.isDirectory) {
    const dirEntry = entry as FileSystemDirectoryEntry
    const entries = await readAllEntries(dirEntry.createReader())
    const nested = await Promise.all(entries.map(collectFiles))
    return nested.flat()
  }
  return []
}

export default function UploadPage() {
  const [results, setResults] = useState<UploadFileResult[]>([])
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const [dragOver, setDragOver] = useState(false)
  const [confirmed, setConfirmed] = useState<Map<string, ConfirmResult>>(
    new Map(),
  )
  const [docs, setDocs] = useState<DocSummary[]>([])

  // Load docs for "new version of existing" option
  useEffect(() => {
    get<DocSummary[]>('/api/docs')
      .then(setDocs)
      .catch(() => {})
  }, [])

  const uploadFiles = useCallback(async (files: FileList | File[]) => {
    setError('')
    setUploading(true)
    setResults([])
    setConfirmed(new Map())
    try {
      const formData = new FormData()
      for (const file of files) {
        formData.append('files', file)
      }
      const data = await postForm<{ files: UploadFileResult[] }>(
        '/api/uploads',
        formData,
      )
      setResults(data.files)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Upload failed')
    } finally {
      setUploading(false)
    }
  }, [])

  async function handleDrop(e: React.DragEvent) {
    e.preventDefault()
    setDragOver(false)

    const items = e.dataTransfer.items
    const entries: FileSystemEntry[] = []

    // Try webkitGetAsEntry to detect directories
    if (items) {
      for (let i = 0; i < items.length; i++) {
        const entry = items[i].webkitGetAsEntry?.()
        if (entry) entries.push(entry)
      }
    }

    if (entries.length > 0) {
      // Recursively collect supported files from entries (handles directories)
      const allNested = await Promise.all(entries.map(collectFiles))
      const files = allNested.flat()
      if (files.length === 0) {
        setError(
          'No supported files found. Supported types: PDF, DOCX, RTF, TXT, JPEG',
        )
        return
      }
      uploadFiles(files)
      return
    }

    // Fallback: filter plain file list by extension
    if (e.dataTransfer.files.length > 0) {
      const supported = Array.from(e.dataTransfer.files).filter((f) =>
        isSupportedFile(f.name),
      )
      if (supported.length === 0) {
        setError(
          'No supported files found. Supported types: PDF, DOCX, RTF, TXT, JPEG',
        )
        return
      }
      uploadFiles(supported)
    }
  }

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    if (e.target.files && e.target.files.length > 0) {
      uploadFiles(e.target.files)
    }
  }

  async function handleConfirm(
    uploadId: string,
    action: 'new_document' | 'new_version',
    existingDocId?: string,
  ) {
    try {
      const body: Record<string, string> = { upload_id: uploadId, action }
      if (existingDocId) body.existing_doc_id = existingDocId
      const result = await post<ConfirmResult>('/api/uploads/confirm', body)
      setConfirmed((prev) => new Map(prev).set(uploadId, result))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Confirm failed')
    }
  }

  return (
    <div>
      <h1 className="mb-4 text-xl font-bold">Upload Documents</h1>

      {error && (
        <div className="mb-4 rounded bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

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
        {uploading ? (
          <p className="text-gray-500 dark:text-gray-400">Uploading...</p>
        ) : (
          <>
            <p className="mb-2 text-gray-600 dark:text-gray-400">
              Drag and drop files or folders here, or click to browse
            </p>
            <p className="mb-4 text-xs text-gray-400">
              PDF, DOCX, RTF, TXT, JPEG supported
            </p>
            <label className="cursor-pointer rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-blue-700">
              Choose Files
              <input
                type="file"
                multiple
                onChange={handleFileChange}
                className="hidden"
                accept=".pdf,.docx,.rtf,.txt,.jpg,.jpeg"
              />
            </label>
          </>
        )}
      </div>

      {results.length > 0 && (
        <div className="mt-6 space-y-3">
          <h2 className="text-lg font-semibold">Upload Results</h2>
          {results.map((r) => (
            <FileResult
              key={r.upload_id}
              result={r}
              confirmed={confirmed.get(r.upload_id)}
              docs={docs}
              onConfirm={handleConfirm}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function FileResult({
  result,
  confirmed,
  docs,
  onConfirm,
}: {
  result: UploadFileResult
  confirmed?: ConfirmResult
  docs: DocSummary[]
  onConfirm: (
    id: string,
    action: 'new_document' | 'new_version',
    docId?: string,
  ) => void
}) {
  const [action, setAction] = useState<'new_document' | 'new_version'>(
    'new_document',
  )
  const [selectedDoc, setSelectedDoc] = useState('')

  if (confirmed) {
    return (
      <div className="rounded-xl border border-green-200 dark:border-green-800 bg-green-50 dark:bg-green-900/20 p-4 shadow-mac">
        <p className="text-sm font-medium text-green-700 dark:text-green-400">
          {result.filename} — Processing started
        </p>
        <Link
          to={`/docs/${confirmed.doc_id}`}
          className="text-sm text-blue-600 dark:text-blue-400 hover:underline"
        >
          View document
        </Link>
      </div>
    )
  }

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

  if (result.status === 'skipped') {
    return (
      <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac p-4">
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {result.filename} — Skipped (unsupported type)
        </p>
      </div>
    )
  }

  return (
    <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac p-4">
      <p className="mb-3 text-sm font-medium">
        {result.filename}{' '}
        <span className="text-gray-400">
          ({(result.size_bytes / 1024).toFixed(0)} KB)
        </span>
      </p>
      <div className="mb-3 space-y-2">
        <label className="flex items-center space-x-2">
          <input
            type="radio"
            name={`action-${result.upload_id}`}
            checked={action === 'new_document'}
            onChange={() => setAction('new_document')}
            className="text-blue-600"
          />
          <span className="text-sm">New document</span>
        </label>
        <label className="flex items-center space-x-2">
          <input
            type="radio"
            name={`action-${result.upload_id}`}
            checked={action === 'new_version'}
            onChange={() => setAction('new_version')}
            className="text-blue-600"
          />
          <span className="text-sm">New version of existing document</span>
        </label>
        {action === 'new_version' && (
          <select
            value={selectedDoc}
            onChange={(e) => setSelectedDoc(e.target.value)}
            className="mt-1 w-full rounded-lg border-0 bg-[var(--color-bg-secondary)] dark:bg-[var(--color-bg-tertiary)] shadow-mac focus:ring-2 focus:ring-[var(--color-accent)]/30 px-3 py-1.5 text-sm"
          >
            <option value="">Select document...</option>
            {docs.map((d) => (
              <option key={d.doc_id} value={d.doc_id}>
                {d.title}
              </option>
            ))}
          </select>
        )}
      </div>
      <button
        onClick={() =>
          onConfirm(
            result.upload_id,
            action,
            action === 'new_version' ? selectedDoc : undefined,
          )
        }
        disabled={action === 'new_version' && !selectedDoc}
        className="rounded-lg bg-blue-600 px-4 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-blue-700 disabled:opacity-50"
      >
        Confirm
      </button>
    </div>
  )
}
