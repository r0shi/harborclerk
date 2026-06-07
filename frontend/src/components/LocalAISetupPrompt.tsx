import { Link } from 'react-router-dom'

type LocalAISetupVariant = 'ask' | 'research'

interface LocalAISetupPromptProps {
  variant: LocalAISetupVariant
  compact?: boolean
  className?: string
}

const COPY: Record<
  LocalAISetupVariant,
  {
    title: string
    description: string
    guidance: string
  }
> = {
  ask: {
    title: 'Set up local AI for Ask',
    description:
      'Ask needs one downloaded and active local AI model before it can answer questions over your documents.',
    guidance:
      'Search, Documents, and Folders still work while you skip setup. Important answers should be checked against citations.',
  },
  research: {
    title: 'Set up local AI for Research',
    description:
      'Research drives a local AI model through repeated searches and tool calls, so it needs an active model first.',
    guidance:
      'Larger models are better for long synthesis. Smaller models are useful for quick lookup, but citations remain the source of truth.',
  },
}

export function LocalAISetupPrompt({ variant, compact = false, className = '' }: LocalAISetupPromptProps) {
  const copy = COPY[variant]

  return (
    <div className={`mx-auto text-center empty-state-appear ${compact ? 'max-w-md' : 'max-w-lg'} ${className}`.trim()}>
      {!compact && (
        <div className="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-2xl bg-amber-50 ring-1 ring-amber-200/60 dark:bg-amber-900/30 dark:ring-amber-700/40">
          <svg
            className="h-8 w-8 text-amber-500 dark:text-amber-400"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={1.5}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09zM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 0 0-2.455 2.456z"
            />
          </svg>
        </div>
      )}

      <h3 className={`${compact ? 'text-[15px]' : 'text-lg'} mb-2 font-semibold text-gray-800 dark:text-gray-200`}>
        {copy.title}
      </h3>
      <p className="mb-3 text-[13px] leading-relaxed text-gray-500 dark:text-gray-400">{copy.description}</p>

      <div className="mb-4 rounded-xl border border-gray-200 bg-gray-50/70 px-4 py-3 text-left text-[12px] text-gray-600 dark:border-gray-700 dark:bg-gray-800/40 dark:text-gray-300">
        <div className="font-medium text-gray-800 dark:text-gray-100">Setup path</div>
        <ol className="mt-2 list-decimal space-y-1 pl-4">
          <li>Open Models.</li>
          <li>Download a model that fits this Mac.</li>
          <li>Activate it when the download completes.</li>
        </ol>
      </div>

      <p className="mb-5 text-[13px] leading-relaxed text-gray-500 dark:text-gray-400">{copy.guidance}</p>

      <div className="flex flex-wrap items-center justify-center gap-2">
        <Link
          to="/settings/models"
          className="inline-flex items-center gap-2 rounded-xl bg-blue-600 px-5 py-2 text-[13px] font-medium text-white shadow-xs transition-colors hover:bg-blue-700"
        >
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3"
            />
          </svg>
          Choose a model
        </Link>
        <Link
          to="/search"
          className="inline-flex items-center rounded-xl border border-gray-300 px-5 py-2 text-[13px] font-medium text-gray-700 transition-colors hover:bg-gray-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800"
        >
          Use Search instead
        </Link>
      </div>

      <p className="mt-4 text-[11px] leading-relaxed text-gray-400 dark:text-gray-500">
        Local AI runs on this machine. Cloud integrations are configured separately and disclose what leaves the device.
      </p>
    </div>
  )
}
