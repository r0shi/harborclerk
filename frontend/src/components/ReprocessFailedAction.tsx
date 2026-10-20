import { useState } from 'react'
import { post } from '../api'

interface ReprocessFailedResult {
  requeued: number
  remaining: number
}

/** A dry run also states the batch size (`limit`) a real call will use; the server owns the default. */
interface ReprocessFailedDryRun extends ReprocessFailedResult {
  limit: number
}

type Phase =
  | { kind: 'idle' }
  | { kind: 'counting' }
  | { kind: 'confirm'; matching: number; limit: number }
  | { kind: 'running'; matching: number; limit: number }
  | { kind: 'done'; requeued: number; remaining: number }
  | { kind: 'nothing' }
  | { kind: 'error'; message: string }

interface Props {
  /** Called after a run that queued anything, so the list drops the documents that left `error`. */
  onDone: () => void
  /**
   * True when the page has filters besides the Failed status (folder, search,
   * type…). The endpoint takes none of them: it covers every failed document in
   * the library, so the confirmation says so rather than implying the view.
   */
  otherFiltersActive?: boolean
}

function plural(n: number, word: string): string {
  return `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`
}

/**
 * "Reprocess all failed" for the Documents page's Failed view (#554). Admin
 * only: the caller renders it for admins, and the endpoint refuses anyone else.
 *
 * The confirmation count comes from the endpoint itself (`dry_run=true`), not
 * from the list's total: the Failed filter also shows documents an admin
 * cancelled or a queue-clear stopped, which the endpoint deliberately leaves
 * alone, so the list's number would promise work that will not happen.
 *
 * One run is one call. Requeued documents leave `error` at once, so a loop on
 * `remaining` would only pick up documents that failed again in the meantime:
 * a permanently broken file failing fast would be requeued over and over. When
 * more than one batch is failed, the result says how many are left and the
 * admin runs it again. The batch size is the `limit` the dry run reports, and
 * the real call sends that same `limit`, so the wording and the run agree
 * without this file restating the server's default.
 *
 * Two-click confirmation, never `window.confirm`: WKWebView in the Mac app
 * returns false from native dialogs without showing them.
 */
export default function ReprocessFailedAction({ onDone, otherFiltersActive = false }: Props) {
  const [phase, setPhase] = useState<Phase>({ kind: 'idle' })

  async function count() {
    setPhase({ kind: 'counting' })
    try {
      const { remaining, limit } = await post<ReprocessFailedDryRun>('/api/docs/reprocess-failed?dry_run=true')
      setPhase(remaining > 0 ? { kind: 'confirm', matching: remaining, limit } : { kind: 'nothing' })
    } catch (e) {
      setPhase({ kind: 'error', message: e instanceof Error ? e.message : 'Could not count failed documents' })
    }
  }

  async function run(matching: number, limit: number) {
    setPhase({ kind: 'running', matching, limit })
    try {
      const result = await post<ReprocessFailedResult>(`/api/docs/reprocess-failed?limit=${limit}`)
      setPhase({ kind: 'done', requeued: result.requeued, remaining: result.remaining })
      if (result.requeued > 0) onDone()
    } catch (e) {
      setPhase({ kind: 'error', message: e instanceof Error ? e.message : 'Reprocess failed' })
    }
  }

  const button =
    'rounded-lg border border-(--color-border) px-2.5 py-1 text-xs font-medium text-(--color-text-primary) hover:bg-black/5 dark:hover:bg-white/5 disabled:opacity-50'

  return (
    <div className="mb-3 flex flex-wrap items-center gap-2 text-xs" data-testid="reprocess-failed">
      {(phase.kind === 'idle' || phase.kind === 'counting') && (
        <button type="button" onClick={count} disabled={phase.kind === 'counting'} className={button}>
          {phase.kind === 'counting' ? 'Counting…' : 'Reprocess all failed'}
        </button>
      )}

      {(phase.kind === 'confirm' || phase.kind === 'running') && (
        <>
          <span className="text-(--color-text-primary)">
            Reprocess all {plural(phase.matching, 'failed document')} in the library?
            {otherFiltersActive && ' Your other filters do not apply: this covers every failed document.'}
            {phase.matching > phase.limit &&
              ` The ${phase.limit.toLocaleString()} most recent are queued now; run it again for the rest.`}
          </span>
          <button
            type="button"
            onClick={() => run(phase.matching, phase.limit)}
            disabled={phase.kind === 'running'}
            className="rounded-lg bg-amber-500 px-2.5 py-1 text-xs font-medium text-white hover:bg-amber-600 disabled:opacity-50"
          >
            {phase.kind === 'running' ? 'Reprocessing…' : 'Confirm reprocess'}
          </button>
          <button
            type="button"
            onClick={() => setPhase({ kind: 'idle' })}
            disabled={phase.kind === 'running'}
            className={button}
          >
            Cancel
          </button>
        </>
      )}

      {phase.kind === 'done' && (
        <>
          <span role="status" className="text-(--color-text-primary)">
            Queued {plural(phase.requeued, 'document')} for reprocessing.
            {phase.remaining > 0 && ` ${plural(phase.remaining, 'failed document')} still waiting.`}
          </span>
          {phase.remaining > 0 ? (
            <button type="button" onClick={count} className={button}>
              Reprocess the rest
            </button>
          ) : (
            <button type="button" onClick={() => setPhase({ kind: 'idle' })} className={button}>
              OK
            </button>
          )}
        </>
      )}

      {phase.kind === 'nothing' && (
        <>
          <span role="status" className="text-(--color-text-secondary)">
            No failed documents to reprocess. Documents that were cancelled or stopped by a queue clear are left alone.
          </span>
          <button type="button" onClick={() => setPhase({ kind: 'idle' })} className={button}>
            OK
          </button>
        </>
      )}

      {phase.kind === 'error' && (
        <>
          <span role="alert" className="text-red-700 dark:text-red-400">
            {phase.message}
          </span>
          <button type="button" onClick={() => setPhase({ kind: 'idle' })} className={button}>
            Dismiss
          </button>
        </>
      )}
    </div>
  )
}
