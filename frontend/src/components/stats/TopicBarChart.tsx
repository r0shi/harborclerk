import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'

interface TopicCluster {
  cluster_id: number
  name: string
  keywords: string[]
  doc_count: number
}

const TICK_STYLE = { fontSize: 11, fill: 'var(--color-chart-tick)' }

export default function TopicBarChart({ topics }: { topics: TopicCluster[] }) {
  const data = [...topics]
    .sort((a, b) => b.doc_count - a.doc_count)
    .slice(0, 15)
    .map((t) => ({
      name: t.name.length > 30 ? t.name.slice(0, 28) + '...' : t.name,
      doc_count: t.doc_count,
    }))

  if (data.length === 0) {
    return <p className="py-8 text-center text-sm text-(--color-text-secondary)">No topics</p>
  }

  return (
    <ResponsiveContainer width="100%" height={Math.max(200, data.length * 28)}>
      <BarChart data={data} layout="vertical" margin={{ left: 10, right: 16, top: 0, bottom: 0 }}>
        <XAxis type="number" hide />
        <YAxis dataKey="name" type="category" width={180} tick={TICK_STYLE} axisLine={false} tickLine={false} />
        <Tooltip
          cursor={false}
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null
            const d = payload[0].payload as { name: string; doc_count: number }
            return (
              <div className="rounded-lg bg-white dark:bg-[#3a3a3c] shadow-mac-lg px-3 py-1.5 text-[12px] text-(--color-text-primary) ring-1 ring-(--color-border)">
                <span className="font-medium">{d.name}</span>: {d.doc_count.toLocaleString()} docs
              </div>
            )
          }}
        />
        <Bar dataKey="doc_count" fill="#007aff" radius={[0, 4, 4, 0]} barSize={14} />
      </BarChart>
    </ResponsiveContainer>
  )
}
