import { Link } from 'react-router-dom'
import { useSystemConfig } from '../hooks/useSystemConfig'
import { useLLMStatusContext } from './LLMStatusBanner'
import { StatusPill, type PillState } from './StatusPill'

interface PillView {
  label: string
  glyph?: string
  state: PillState
  title: string
  to: string
}

function statusView({
  healthStatus,
  loaded,
  llmState,
}: {
  healthStatus: 'healthy' | 'degraded' | null
  loaded: boolean
  llmState: string
}): PillView {
  if (healthStatus === 'degraded') {
    return {
      label: 'Needs attention',
      glyph: '⚠',
      state: 'error',
      title: 'System checks need attention',
      to: '/settings/status',
    }
  }

  if (!loaded) {
    return {
      label: 'Checking',
      state: 'pending',
      title: 'Checking system status',
      to: '/settings/status',
    }
  }

  if (llmState === 'loading') {
    return {
      label: 'AI loading',
      state: 'running',
      title: 'Local AI model is loading',
      to: '/settings/models',
    }
  }

  if (llmState === 'deactivated') {
    return {
      label: 'Choose model',
      state: 'idle',
      title: 'No local AI model is active',
      to: '/settings/models',
    }
  }

  if (llmState === 'unknown') {
    return {
      label: 'AI unknown',
      state: 'pending',
      title: 'Local AI status is unknown',
      to: '/settings/models',
    }
  }

  return {
    label: 'Ready',
    glyph: '●',
    state: 'active',
    title: 'System ready',
    to: '/settings/status',
  }
}

export default function GlobalStatusPill() {
  const { status } = useLLMStatusContext()
  const systemConfig = useSystemConfig()
  const view = statusView({
    healthStatus: systemConfig.healthStatus,
    loaded: systemConfig.loaded,
    llmState: status.state,
  })

  return (
    <Link
      to={view.to}
      aria-label={`Status: ${view.label}`}
      title={view.title}
      className="inline-flex rounded-full transition-opacity hover:opacity-80 focus:outline-hidden focus:ring-2 focus:ring-(--color-accent)/30"
    >
      <StatusPill state={view.state} label={view.label} glyph={view.glyph} />
    </Link>
  )
}
