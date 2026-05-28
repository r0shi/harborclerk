interface TopicCluster {
  cluster_id: number
  name: string
  keywords: string[]
  doc_count: number
}

// Bondi Blue — classic early OS X. Purely decorative accent; the dot
// doesn't encode anything about the topic.
const DOT_COLOR = '#5ac8fa'

export default function TopicKeywords({ topics }: { topics: TopicCluster[] }) {
  if (topics.length === 0) {
    return <p className="py-8 text-center text-sm text-(--color-text-secondary)">No topics</p>
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 max-h-[36rem] overflow-y-auto pr-1">
      {topics.map((topic) => (
        <div key={topic.cluster_id} className="rounded-lg bg-(--color-bg-secondary) p-3">
          <div className="flex items-center gap-2 mb-2">
            <span className="inline-block h-2.5 w-2.5 rounded-full shrink-0" style={{ backgroundColor: DOT_COLOR }} />
            <h4 className="text-[13px] font-medium text-(--color-text-primary) leading-snug">{topic.name}</h4>
          </div>
          <p className="text-[11px] text-(--color-text-secondary) mb-1.5">
            {topic.doc_count.toLocaleString()} documents
          </p>
          <div className="flex flex-wrap gap-1">
            {topic.keywords.map((kw) => (
              <span
                key={kw}
                className="rounded-full bg-white dark:bg-[#3a3a3c] px-2 py-0.5 text-[11px] text-(--color-text-secondary) ring-1 ring-(--color-border)"
              >
                {kw}
              </span>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
