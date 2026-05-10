import { useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import type { DocumentQueueItem, CompletedItem } from '../../hooks/useQueueTray'
import DocumentRow from './DocumentRow'
import CompletedRow from './CompletedRow'
import PipelineTab from './PipelineTab'
import SummarizingRow, { type SummarizingItem } from './SummarizingRow'

interface QueuePanelProps {
  activeItems: Map<string, DocumentQueueItem>
  completed: CompletedItem[]
  summarizing: SummarizingItem[]
  onClose: () => void
}

type TabId = 'active' | 'pipeline'

// Threshold above which we virtualize the Active list. Below this, the
// classic non-virtualized layout is fine and avoids absolute-positioning
// quirks for tiny lists.
const VIRTUALIZE_THRESHOLD = 30

export default function QueuePanel({ activeItems, completed, summarizing, onClose }: QueuePanelProps) {
  const [exiting, setExiting] = useState(false)
  const [tab, setTab] = useState<TabId>('active')
  const scrollRef = useRef<HTMLDivElement>(null)

  const handleClose = () => {
    setExiting(true)
  }

  const handleAnimationEnd = (e: React.AnimationEvent) => {
    if (e.animationName === 'panelSlideDown') {
      setExiting(false)
      onClose()
    }
  }

  // Map iteration order matches insertion order (Map.set preserves order
  // when the key already exists). The hook always sets the same
  // enqueued_at value for an existing doc, so iteration order matches
  // enqueued-time order without an explicit sort.
  const active = Array.from(activeItems.values())
  const completedDone = completed.filter((c) => c.status === 'done')
  const completedErrors = completed.filter((c) => c.status === 'error')

  const hasActive = active.length > 0
  const hasCompleted = completedDone.length > 0
  const hasErrors = completedErrors.length > 0
  const activeCount = active.length
  const shouldVirtualize = activeCount > VIRTUALIZE_THRESHOLD

  // The virtualizer is always created (rules of hooks) but only used
  // when shouldVirtualize is true. estimateSize is the collapsed
  // DocumentRow height; measureElement re-measures on mount/expand.
  const virtualizer = useVirtualizer({
    count: shouldVirtualize ? activeCount : 0,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 76,
    overscan: 4,
  })

  // Sibling virtualizer for the Summarizing section. SummarizingRow is a
  // single-line row (~28px) so the estimate is much smaller than the
  // collapsed DocumentRow. Reuses the same scrollRef.
  const summarizeVirtualizer = useVirtualizer({
    count: summarizing.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 28,
    overscan: 6,
  })

  return (
    <div className={`mb-2 ${exiting ? 'panel-exit' : 'panel-enter'}`} onAnimationEnd={handleAnimationEnd}>
      {/* Fixed height keeps the drawer from jumping vertically when the user
          switches between Active and Pipeline tabs. The 60vh cap keeps it
          reasonable on short windows; on tall screens the drawer is a stable
          540px regardless of which tab's content is shorter. */}
      <div className="w-[360px] h-[540px] max-h-[60vh] flex flex-col rounded-2xl bg-(--bg-vibrancy) backdrop-blur-xl shadow-mac-lg ring-1 ring-(--color-border) overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-(--color-border)">
          <span className="text-[13px] font-semibold text-(--color-text-primary)">Processing</span>
          <button
            onClick={handleClose}
            aria-label="Tuck drawer away"
            title="Tuck away"
            className="rounded-md p-0.5 text-(--color-text-secondary) hover:text-(--color-text-primary) hover:bg-black/4 dark:hover:bg-white/6 transition-colors"
          >
            {/* Chevron-down: dismissal reads as "tuck away into a drawer",
                not "destroy". The drawer reopens with current state when
                the queue pill is clicked again. */}
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
            </svg>
          </button>
        </div>

        {/* Tab bar */}
        <div className="flex border-b border-(--color-border) px-2">
          <TabButton id="active" current={tab} onClick={setTab}>
            Active{activeCount > 0 && <span className="ml-1 tabular-nums">({activeCount})</span>}
          </TabButton>
          <TabButton id="pipeline" current={tab} onClick={setTab}>
            Pipeline
          </TabButton>
        </div>

        {/* Body */}
        <div ref={scrollRef} className="overflow-y-auto queue-panel-scroll flex-1">
          {tab === 'pipeline' && <PipelineTab />}
          {tab === 'active' && (
            <>
              {/* Active section. Virtualized once we cross VIRTUALIZE_THRESHOLD
                  rows so 10K-doc ingests don't render 10K <DocumentRow>s on
                  every SSE event. */}
              {hasActive && shouldVirtualize && (
                <div className="px-4 py-2">
                  <div className="text-[11px] font-medium uppercase tracking-wider text-(--color-text-secondary) mb-2">
                    Active
                  </div>
                  <div style={{ height: `${virtualizer.getTotalSize()}px`, position: 'relative', width: '100%' }}>
                    {virtualizer.getVirtualItems().map((virtualItem) => {
                      const item = active[virtualItem.index]
                      return (
                        <div
                          key={item.doc_id}
                          ref={virtualizer.measureElement}
                          data-index={virtualItem.index}
                          style={{
                            position: 'absolute',
                            top: 0,
                            left: 0,
                            width: '100%',
                            transform: `translateY(${virtualItem.start}px)`,
                          }}
                        >
                          <DocumentRow item={item} />
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}

              {/* Active section, non-virtualized fast path for small lists */}
              {hasActive && !shouldVirtualize && (
                <div className="px-4 py-2">
                  <div className="text-[11px] font-medium uppercase tracking-wider text-(--color-text-secondary) mb-2">
                    Active
                  </div>
                  <div className="space-y-1">
                    {active.map((item) => (
                      <DocumentRow key={item.doc_id} item={item} />
                    ))}
                  </div>
                </div>
              )}

              {/* Summarizing section — sits between Active and Completed.
                  Hidden entirely when summarizing is empty (no header, no
                  divider). Virtualized at >VIRTUALIZE_THRESHOLD rows (30), same as Active.
                  Failed-summary jobs land in Errors via the existing
                  CompletedItem path, not here. */}
              {summarizing.length > 0 && (
                <>
                  {hasActive && <div className="border-b border-(--color-border)" />}
                  <div className="px-4 py-2">
                    <div className="text-[11px] font-medium uppercase tracking-wider mb-2" style={{ color: '#c4a4ff' }}>
                      Summarizing ({summarizing.length})
                    </div>
                    {/*
                      Two virtualizers sharing the same scrollRef can't
                      compute independent visible windows — they each see
                      the parent scroll's scrollTop and render items
                      positioned around it, regardless of which section
                      is actually in view. To avoid the collision, only
                      virtualize Summarizing when Active is NOT
                      virtualized. With both sections >30 items
                      simultaneously (rare for a single-tenant
                      deployment), Summarizing renders flat — slower but
                      visually correct. The proper fix is a single
                      virtualizer over the merged list or independent
                      scroll containers, both larger refactors.
                    */}
                    {summarizing.length > VIRTUALIZE_THRESHOLD && !shouldVirtualize ? (
                      <div
                        style={{
                          height: `${summarizeVirtualizer.getTotalSize()}px`,
                          position: 'relative',
                          width: '100%',
                        }}
                      >
                        {summarizeVirtualizer.getVirtualItems().map((vi) => {
                          const item = summarizing[vi.index]
                          return (
                            <div
                              key={item.doc_id}
                              ref={summarizeVirtualizer.measureElement}
                              data-index={vi.index}
                              style={{
                                position: 'absolute',
                                top: 0,
                                left: 0,
                                width: '100%',
                                transform: `translateY(${vi.start}px)`,
                              }}
                            >
                              <SummarizingRow item={item} />
                            </div>
                          )
                        })}
                      </div>
                    ) : (
                      <div className="space-y-0.5">
                        {summarizing.map((item) => (
                          <SummarizingRow key={item.doc_id} item={item} />
                        ))}
                      </div>
                    )}
                  </div>
                </>
              )}

              {/* Divider */}
              {(hasActive || summarizing.length > 0) && hasCompleted && (
                <div className="border-b border-(--color-border)" />
              )}

              {/* Completed section (done items only) */}
              {hasCompleted && (
                <div className="px-4 py-2">
                  <div className="text-[11px] font-medium uppercase tracking-wider text-(--color-text-secondary) mb-2">
                    Completed ({completedDone.length})
                  </div>
                  <div className="space-y-2">
                    {completedDone.map((item) => (
                      <CompletedRow key={item.doc_id} item={item} />
                    ))}
                  </div>
                </div>
              )}

              {/* Divider */}
              {(hasActive || summarizing.length > 0 || hasCompleted) && hasErrors && (
                <div className="border-b border-(--color-border)" />
              )}

              {/* Errors section */}
              {hasErrors && (
                <div className="px-4 py-2">
                  <div className="text-[11px] font-medium uppercase tracking-wider text-red-500/80 mb-2">
                    Errors ({completedErrors.length})
                  </div>
                  <div className="space-y-2">
                    {completedErrors.map((item) => (
                      <CompletedRow key={item.doc_id} item={item} />
                    ))}
                  </div>
                </div>
              )}

              {/* Empty state */}
              {!hasActive && summarizing.length === 0 && !hasCompleted && !hasErrors && (
                <div className="px-4 py-6 text-center text-[13px] text-(--color-text-secondary)">No items in queue</div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

interface TabButtonProps {
  id: TabId
  current: TabId
  onClick: (id: TabId) => void
  children: React.ReactNode
}

function TabButton({ id, current, onClick, children }: TabButtonProps) {
  const active = id === current
  return (
    <button
      onClick={() => onClick(id)}
      className={`relative px-3 py-2 text-[12px] font-medium transition-colors ${
        active ? 'text-(--color-text-primary)' : 'text-(--color-text-secondary) hover:text-(--color-text-primary)'
      }`}
    >
      {children}
      {active && <span className="absolute inset-x-2 bottom-0 h-0.5 rounded-full bg-(--color-accent)" />}
    </button>
  )
}
