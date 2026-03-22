import { Treemap, ResponsiveContainer, Tooltip } from 'recharts'

interface TopicCluster {
  cluster_id: number
  name: string
  keywords: string[]
  doc_count: number
}

const PIE_COLORS = ['#007aff', '#34c759', '#ff9500', '#ff3b30', '#af52de', '#5ac8fa', '#ff2d55', '#ffcc00']

interface CellProps {
  x: number
  y: number
  width: number
  height: number
  name?: string
  index?: number
}

function TreemapCell(props: CellProps) {
  const { x, y, width, height, name, index } = props
  const fill = PIE_COLORS[(index ?? 0) % PIE_COLORS.length]
  return (
    <g>
      <rect x={x} y={y} width={width} height={height} fill={fill} rx={4} opacity={0.85} />
      {width > 60 && height > 30 && (
        <text
          x={x + width / 2}
          y={y + height / 2}
          textAnchor="middle"
          dominantBaseline="central"
          fill="#fff"
          fontSize={11}
          fontWeight={500}
        >
          {name && name.length > Math.floor(width / 7) ? name.slice(0, Math.floor(width / 7) - 1) + '...' : name}
        </text>
      )}
    </g>
  )
}

export default function TopicTreemap({ topics }: { topics: TopicCluster[] }) {
  const data = topics
    .filter((t) => t.doc_count > 0)
    .map((t) => ({
      name: t.name,
      doc_count: t.doc_count,
      keywords: t.keywords,
    }))

  if (data.length === 0) {
    return <p className="py-8 text-center text-sm text-(--color-text-secondary)">No topics</p>
  }

  return (
    <ResponsiveContainer width="100%" height={300}>
      <Treemap
        data={data}
        dataKey="doc_count"
        nameKey="name"
        content={<TreemapCell x={0} y={0} width={0} height={0} />}
      >
        <Tooltip
          content={({ active, payload }) => {
            if (!active || !payload?.length) return null
            const d = payload[0].payload as TopicCluster
            return (
              <div className="rounded-lg bg-white dark:bg-[#3a3a3c] shadow-mac-lg px-3 py-1.5 text-[12px] text-(--color-text-primary) ring-1 ring-(--color-border)">
                <p className="font-medium">{d.name}</p>
                <p>{d.doc_count.toLocaleString()} docs</p>
                {d.keywords && <p className="mt-0.5 text-(--color-text-secondary)">{d.keywords.join(', ')}</p>}
              </div>
            )
          }}
        />
      </Treemap>
    </ResponsiveContainer>
  )
}
