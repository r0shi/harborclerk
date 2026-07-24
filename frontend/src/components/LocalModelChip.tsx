import { Link } from 'react-router'
import { modelGuidance } from '../utils/modelGuidance'

interface LocalModelChipModel {
  id: string
  name?: string | null
  size_bytes?: number
  supports_research?: boolean
}

interface LocalModelChipProps {
  model: LocalModelChipModel
  label?: string
}

export function LocalModelChip({ model, label = 'Local AI' }: LocalModelChipProps) {
  const guidance =
    typeof model.size_bytes === 'number' && typeof model.supports_research === 'boolean'
      ? modelGuidance({ id: model.id, size_bytes: model.size_bytes, supports_research: model.supports_research })
      : null
  const modelName = model.name || model.id
  const title = guidance
    ? `${modelName}: ${guidance.label}. ${guidance.note}${guidance.warning ? ` ${guidance.warning}` : ''}`
    : `${modelName}: active local AI model`

  return (
    <Link
      to="/settings/models"
      title={title}
      className="inline-flex max-w-full items-center gap-1.5 rounded-full border border-gray-200 bg-white/70 px-2.5 py-1 text-[11px] text-gray-500 transition-colors hover:border-gray-300 hover:text-gray-700 dark:border-gray-700 dark:bg-gray-800/70 dark:text-gray-400 dark:hover:border-gray-600 dark:hover:text-gray-200"
    >
      <span className="shrink-0 font-medium text-gray-600 dark:text-gray-300">{label}</span>
      <span aria-hidden className="text-gray-300 dark:text-gray-600">
        ·
      </span>
      <span className="min-w-0 truncate">{modelName}</span>
      {guidance && (
        <>
          <span aria-hidden className="text-gray-300 dark:text-gray-600">
            ·
          </span>
          <span className="shrink-0 text-gray-400 dark:text-gray-500">{guidance.label}</span>
          {guidance.warning && (
            <span
              aria-label={guidance.warning}
              className="shrink-0 rounded-full bg-amber-100 px-1 text-[10px] font-semibold text-amber-700 dark:bg-amber-900/30 dark:text-amber-400"
            >
              !
            </span>
          )}
        </>
      )}
    </Link>
  )
}
