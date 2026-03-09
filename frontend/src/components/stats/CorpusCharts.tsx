import {
  PieChart,
  Pie,
  Cell,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  AreaChart,
  Area,
  ResponsiveContainer,
} from 'recharts'
import { InfoTip } from '../InfoTip'

interface CorpusStats {
  languages: Record<string, number>
  mime_types: Record<string, number>
  ocr_breakdown: { born_digital: number; ocr_used: number; unknown: number }
  size_buckets: { label: string; count: number }[]
  growth_timeline: { month: string; count: number }[]
  pipeline_timing: Record<string, { avg_secs: number; count: number }>
  entity_type_counts: Record<string, number>
  top_entities: { text: string; type: string; mentions: number }[]
}

const PIE_COLORS = ['#007aff', '#34c759', '#ff9500', '#ff3b30', '#af52de', '#5ac8fa', '#ff2d55', '#ffcc00']

export const ENTITY_COLORS: Record<string, string> = {
  PERSON: '#007aff',
  ORG: '#34c759',
  GPE: '#ff9500',
  LOC: '#af52de',
  DATE: '#5ac8fa',
  EVENT: '#ff2d55',
  WORK_OF_ART: '#ffcc00',
  FAC: '#ff3b30',
  NORP: '#30d158',
  PRODUCT: '#64d2ff',
}

export const ENTITY_TYPE_LABELS: Record<string, string> = {
  PERSON: 'Person',
  ORG: 'Organization',
  GPE: 'Country / City / State',
  LOC: 'Location',
  DATE: 'Date',
  EVENT: 'Event',
  FAC: 'Facility',
  PRODUCT: 'Product',
  WORK_OF_ART: 'Work of Art',
  LAW: 'Law / Regulation',
  NORP: 'Nationality / Religion / Political Group',
  LANGUAGE: 'Language',
  MONEY: 'Monetary Value',
  CARDINAL: 'Cardinal Number',
  ORDINAL: 'Ordinal Number',
  QUANTITY: 'Quantity / Measurement',
  PERCENT: 'Percentage',
  TIME: 'Time',
}

function entityColor(type: string): string {
  return ENTITY_COLORS[type] || '#98989d'
}

const TICK_STYLE = { fontSize: 10, fill: 'var(--color-chart-tick)' }
const TICK_STYLE_11 = { fontSize: 11, fill: 'var(--color-chart-tick)' }

function ChartCard({ title, children, tip }: { title: string; children: React.ReactNode; tip?: string }) {
  return (
    <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) p-4">
      <h3 className="mb-3 text-[13px] font-semibold text-(--color-text-primary)">
        {title}
        {tip && <InfoTip text={tip} />}
      </h3>
      {children}
    </div>
  )
}

function shortMime(mime: string): string {
  const map: Record<string, string> = {
    'application/pdf': 'PDF',
    'image/jpeg': 'JPEG',
    'image/png': 'PNG',
    'image/tiff': 'TIFF',
    'text/plain': 'TXT',
    'text/csv': 'CSV',
    'text/markdown': 'MD',
    'text/html': 'HTML',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'DOCX',
    'application/msword': 'DOC',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'XLSX',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'PPTX',
    'application/rtf': 'RTF',
    'application/epub+zip': 'EPUB',
    'application/vnd.oasis.opendocument.text': 'ODT',
    'message/rfc822': 'EML',
  }
  return map[mime] || mime.split('/').pop()?.toUpperCase() || mime
}

function CustomTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean
  payload?: { value: number }[]
  label?: string
}) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-lg bg-white dark:bg-[#3a3a3c] shadow-mac-lg px-3 py-1.5 text-[12px] text-(--color-text-primary) ring-1 ring-(--color-border)">
      <span className="font-medium">{label}</span>: {payload[0].value.toLocaleString()}
    </div>
  )
}

function PieTooltip({
  active,
  payload,
}: {
  active?: boolean
  payload?: { name: string; value: number; payload: { percent: number } }[]
}) {
  if (!active || !payload?.length) return null
  const d = payload[0]
  return (
    <div className="rounded-lg bg-white dark:bg-[#3a3a3c] shadow-mac-lg px-3 py-1.5 text-[12px] text-(--color-text-primary) ring-1 ring-(--color-border)">
      <span className="font-medium">{d.name}</span>: {d.value.toLocaleString()} ({(d.payload.percent * 100).toFixed(0)}
      %)
    </div>
  )
}

export default function CorpusCharts({ stats }: { stats: CorpusStats }) {
  // Language pie data
  const langData = Object.entries(stats.languages).map(([name, value]) => ({ name, value }))

  // Mime bar data
  const mimeData = Object.entries(stats.mime_types)
    .sort((a, b) => b[1] - a[1])
    .map(([mime, count]) => ({ name: shortMime(mime), count }))

  // OCR pie data
  const ocrData = [
    { name: 'Born Digital', value: stats.ocr_breakdown.born_digital },
    { name: 'OCR Used', value: stats.ocr_breakdown.ocr_used },
  ].filter((d) => d.value > 0)
  if (stats.ocr_breakdown.unknown > 0) {
    ocrData.push({ name: 'Pending', value: stats.ocr_breakdown.unknown })
  }

  // Pipeline timing bar data
  const stageOrder = ['extract', 'ocr', 'chunk', 'entities', 'embed', 'summarize', 'finalize']
  const timingData = stageOrder
    .filter((s) => stats.pipeline_timing[s])
    .map((s) => ({
      name: s,
      avg_secs: stats.pipeline_timing[s].avg_secs,
    }))

  // Cumulative growth
  const growthData = stats.growth_timeline.reduce<{ month: string; total: number }[]>((acc, d) => {
    const prev = acc.length > 0 ? acc[acc.length - 1].total : 0
    acc.push({ month: d.month, total: prev + d.count })
    return acc
  }, [])

  // Top entities bar
  const topEntitiesData = stats.top_entities.map((e) => ({
    name: e.text.length > 20 ? e.text.slice(0, 18) + '...' : e.text,
    mentions: e.mentions,
    type: e.type,
    fill: entityColor(e.type),
  }))

  return (
    <>
      {/* 6-chart grid */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <ChartCard title="Languages">
          {langData.length > 0 ? (
            <ResponsiveContainer width="100%" height={180}>
              <PieChart>
                <Pie data={langData} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={65} strokeWidth={0}>
                  {langData.map((_, i) => (
                    <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip content={<PieTooltip />} />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <p className="py-8 text-center text-sm text-(--color-text-secondary)">No data</p>
          )}
          {langData.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-(--color-text-secondary)">
              {langData.map((d, i) => (
                <span key={d.name} className="flex items-center gap-1">
                  <span
                    className="inline-block h-2 w-2 rounded-full"
                    style={{ backgroundColor: PIE_COLORS[i % PIE_COLORS.length] }}
                  />
                  {d.name}
                </span>
              ))}
            </div>
          )}
        </ChartCard>

        <ChartCard title="File Types">
          {mimeData.length > 0 ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={mimeData} layout="vertical" margin={{ left: 0, right: 8, top: 0, bottom: 0 }}>
                <XAxis type="number" hide />
                <YAxis
                  dataKey="name"
                  type="category"
                  width={50}
                  tick={TICK_STYLE_11}
                  axisLine={false}
                  tickLine={false}
                />
                <Tooltip content={<CustomTooltip />} cursor={false} />
                <Bar dataKey="count" fill="#007aff" radius={[0, 4, 4, 0]} barSize={14} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <p className="py-8 text-center text-sm text-(--color-text-secondary)">No data</p>
          )}
        </ChartCard>

        <ChartCard
          title="OCR Breakdown"
          tip="OCR (Optical Character Recognition) extracts text from scanned images. 'Born digital' means the document already contained selectable text (e.g. a Word file saved as PDF) and didn't need OCR."
        >
          {ocrData.length > 0 ? (
            <ResponsiveContainer width="100%" height={180}>
              <PieChart>
                <Pie
                  data={ocrData}
                  dataKey="value"
                  nameKey="name"
                  cx="50%"
                  cy="50%"
                  innerRadius={40}
                  outerRadius={65}
                  strokeWidth={0}
                >
                  {ocrData.map((_, i) => (
                    <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip content={<PieTooltip />} />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <p className="py-8 text-center text-sm text-(--color-text-secondary)">No data</p>
          )}
          {ocrData.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-(--color-text-secondary)">
              {ocrData.map((d, i) => (
                <span key={d.name} className="flex items-center gap-1">
                  <span
                    className="inline-block h-2 w-2 rounded-full"
                    style={{ backgroundColor: PIE_COLORS[i % PIE_COLORS.length] }}
                  />
                  {d.name}
                </span>
              ))}
            </div>
          )}
        </ChartCard>

        <ChartCard title="Document Sizes">
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={stats.size_buckets} margin={{ left: -10, right: 8, top: 0, bottom: 0 }}>
              <XAxis dataKey="label" tick={TICK_STYLE} axisLine={false} tickLine={false} />
              <YAxis tick={TICK_STYLE} axisLine={false} tickLine={false} />
              <Tooltip content={<CustomTooltip />} cursor={false} />
              <Bar dataKey="count" fill="#34c759" radius={[4, 4, 0, 0]} barSize={20} />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title="Corpus Growth">
          {growthData.length > 0 ? (
            <ResponsiveContainer width="100%" height={180}>
              <AreaChart data={growthData} margin={{ left: -10, right: 8, top: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="growthGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#007aff" stopOpacity={0.2} />
                    <stop offset="100%" stopColor="#007aff" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <XAxis dataKey="month" tick={TICK_STYLE} axisLine={false} tickLine={false} />
                <YAxis tick={TICK_STYLE} axisLine={false} tickLine={false} />
                <Tooltip content={<CustomTooltip />} />
                <Area type="monotone" dataKey="total" stroke="#007aff" fill="url(#growthGrad)" strokeWidth={2} />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <p className="py-8 text-center text-sm text-(--color-text-secondary)">No data</p>
          )}
        </ChartCard>

        <ChartCard
          title="Avg. Pipeline Timing"
          tip="Average time spent in each processing step when a document is ingested — extracting text, running OCR, splitting into chunks, identifying entities, generating embeddings, and summarizing."
        >
          {timingData.length > 0 ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={timingData} margin={{ left: -10, right: 8, top: 0, bottom: 0 }}>
                <XAxis dataKey="name" tick={TICK_STYLE} axisLine={false} tickLine={false} />
                <YAxis tick={TICK_STYLE} axisLine={false} tickLine={false} unit="s" />
                <Tooltip
                  cursor={false}
                  content={({ active, payload, label }) => {
                    if (!active || !payload?.length) return null
                    return (
                      <div className="rounded-lg bg-white dark:bg-[#3a3a3c] shadow-mac-lg px-3 py-1.5 text-[12px] text-(--color-text-primary) ring-1 ring-(--color-border)">
                        <span className="font-medium">{label}</span>: {Number(payload[0].value).toFixed(1)}s
                      </div>
                    )
                  }}
                />
                <Bar dataKey="avg_secs" fill="#ff9500" radius={[4, 4, 0, 0]} barSize={20} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <p className="py-8 text-center text-sm text-(--color-text-secondary)">No data</p>
          )}
        </ChartCard>
      </div>

      {/* Top Entities */}
      {topEntitiesData.length > 0 && (
        <div className="mt-4 rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) p-4">
          <h3 className="mb-3 text-[13px] font-semibold text-(--color-text-primary)">
            Top Entities
            <InfoTip text="The most frequently mentioned named entities across your documents, grouped by type. Entities are people, organizations, places, dates, and other proper nouns identified automatically." />
          </h3>
          <div className="mb-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-(--color-text-secondary)">
            {[...new Set(topEntitiesData.map((e) => e.type))].map((type) => (
              <span
                key={type}
                className="group relative flex items-center gap-1 cursor-default"
                title={ENTITY_TYPE_LABELS[type] ?? type}
              >
                <span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: entityColor(type) }} />
                {type}
                <span className="pointer-events-none absolute bottom-full left-1/2 mb-1 -translate-x-1/2 whitespace-nowrap rounded bg-gray-800 dark:bg-gray-700 px-1.5 py-0.5 text-[10px] text-white opacity-0 transition-opacity group-hover:opacity-100">
                  {ENTITY_TYPE_LABELS[type] ?? type}
                </span>
              </span>
            ))}
          </div>
          <ResponsiveContainer width="100%" height={Math.max(200, topEntitiesData.length * 24)}>
            <BarChart data={topEntitiesData} layout="vertical" margin={{ left: 10, right: 16, top: 0, bottom: 0 }}>
              <XAxis type="number" hide />
              <YAxis
                dataKey="name"
                type="category"
                width={140}
                tick={TICK_STYLE_11}
                axisLine={false}
                tickLine={false}
              />
              <Tooltip
                cursor={false}
                content={({ active, payload }) => {
                  if (!active || !payload?.length) return null
                  const d = payload[0].payload as { name: string; mentions: number; type: string }
                  return (
                    <div className="rounded-lg bg-white dark:bg-[#3a3a3c] shadow-mac-lg px-3 py-1.5 text-[12px] text-(--color-text-primary) ring-1 ring-(--color-border)">
                      <span className="font-medium">{d.name}</span> ({ENTITY_TYPE_LABELS[d.type] ?? d.type}):{' '}
                      {d.mentions.toLocaleString()} mentions
                    </div>
                  )
                }}
              />
              <Bar dataKey="mentions" radius={[0, 4, 4, 0]} barSize={14}>
                {topEntitiesData.map((entry, i) => (
                  <Cell key={i} fill={entry.fill} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </>
  )
}
