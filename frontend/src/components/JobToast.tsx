import { useCallback, useState } from 'react'
import { useJobEvents, type JobEvent } from '../hooks/useJobEvents'

interface ActiveJob {
  version_id: string
  stage: string
  status: string
  progress_current?: number
  progress_total?: number
}

export default function JobToast() {
  const [jobs, setJobs] = useState<Map<string, ActiveJob>>(new Map())

  const onEvent = useCallback((event: JobEvent) => {
    const key = `${event.version_id}:${event.stage}`

    setJobs((prev) => {
      const next = new Map(prev)
      if (event.status === 'done' || event.status === 'error') {
        next.delete(key)
      } else {
        next.set(key, {
          version_id: event.version_id,
          stage: event.stage,
          status: event.status,
          progress_current: event.progress,
          progress_total: event.total,
        })
      }
      return next
    })
  }, [])

  useJobEvents(onEvent)

  if (jobs.size === 0) return null

  return (
    <div className="fixed bottom-4 right-4 z-50 space-y-2">
      {Array.from(jobs.values()).map((job) => (
        <div
          key={`${job.version_id}:${job.stage}`}
          className="rounded-xl bg-[var(--bg-vibrancy)] backdrop-blur-xl dark:bg-[var(--bg-vibrancy)] px-4 py-3 shadow-mac-lg ring-1 ring-[var(--color-border)]"
        >
          <div className="flex items-center space-x-2">
            <div className="h-2 w-2 animate-pulse rounded-full bg-amber-500" />
            <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
              {stageLabel(job.stage)}
            </span>
          </div>
          {job.progress_total != null && job.progress_total > 0 && (
            <div className="mt-1.5">
              <div className="h-1.5 w-48 rounded-full bg-gray-200 dark:bg-gray-600">
                <div
                  className="h-1.5 rounded-full bg-blue-500 transition-all"
                  style={{
                    width: `${Math.round(((job.progress_current || 0) / job.progress_total) * 100)}%`,
                  }}
                />
              </div>
              <span className="text-xs text-gray-500 dark:text-gray-400">
                {job.progress_current}/{job.progress_total}
              </span>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function stageLabel(stage: string): string {
  switch (stage) {
    case 'extract':
      return 'Extracting'
    case 'ocr':
      return 'OCR processing'
    case 'chunk':
      return 'Chunking'
    case 'embed':
      return 'Embedding'
    case 'finalize':
      return 'Finalizing'
    default:
      return stage
  }
}
