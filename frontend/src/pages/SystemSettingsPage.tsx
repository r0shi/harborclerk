import { Link } from 'react-router-dom'
import { useAuth } from '../auth'
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'
import { IconTile } from '../components/IconTile'
import type { AreaHue } from '../hooks/useAreaAccent'

interface SettingsItem {
  to: string
  label: string
  sub: string
  icon: string
  hue: AreaHue
}

const SECTIONS: { label: string; items: SettingsItem[] }[] = [
  {
    label: 'Personal',
    items: [
      {
        to: '/preferences',
        label: 'Preferences',
        sub: 'Theme, page size, and account settings',
        icon: '👤',
        hue: 'settings',
      },
    ],
  },
]

const ADMIN_SECTIONS: { label: string; items: SettingsItem[] }[] = [
  {
    label: 'Integrations',
    items: [
      {
        to: '/settings/integrations',
        label: 'Integrations',
        sub: 'MCP, CLI, and external AI tool setup',
        icon: '🔌',
        hue: 'research',
      },
    ],
  },
  {
    label: 'Access & identity',
    items: [
      { to: '/settings/users', label: 'Users', sub: 'Manage accounts and roles', icon: '👥', hue: 'docs' },
      {
        to: '/settings/api-keys',
        label: 'API Keys',
        sub: 'Create, scope, and revoke API keys',
        icon: '🔑',
        hue: 'research',
      },
    ],
  },
  {
    label: 'Models & languages',
    items: [
      {
        to: '/settings/models',
        label: 'Models',
        sub: 'Download and manage local AI models',
        icon: '🧠',
        hue: 'observatory',
      },
      {
        to: '/settings/languages',
        label: 'Languages',
        sub: 'OCR and entity language packs',
        icon: '🌐',
        hue: 'explore',
      },
    ],
  },
  {
    label: 'Behavior & limits',
    items: [
      {
        to: '/settings/retrieval',
        label: 'Retrieval',
        sub: 'Ask, Research, MCP, and search behavior',
        icon: '🔍',
        hue: 'search',
      },
      { to: '/settings/rate-limits', label: 'Rate Limits', sub: 'Default API key rate limits', icon: '⏱', hue: 'ask' },
    ],
  },
  {
    label: 'Operations',
    items: [
      {
        to: '/settings/status',
        label: 'Status',
        sub: 'Readiness, service health, and mail account state',
        icon: '💚',
        hue: 'observatory',
      },
      {
        to: '/settings/diagnostics',
        label: 'Diagnostics',
        sub: 'Logs and advanced troubleshooting detail',
        icon: '📜',
        hue: 'settings',
      },
      {
        to: '/settings/maintenance',
        label: 'Maintenance',
        sub: 'Purge, reaper, and cleanup',
        icon: '🧹',
        hue: 'ask',
      },
      {
        to: '/settings/security',
        label: 'Security',
        sub: 'Encryption status and master key management',
        icon: '🔒',
        hue: 'research',
      },
    ],
  },
]

export default function SystemSettingsPage() {
  const { isAdmin } = useAuth()
  const sections = isAdmin ? [...SECTIONS, ...ADMIN_SECTIONS] : SECTIONS

  return (
    <div className="mx-auto max-w-4xl px-4 py-8">
      <PageHeader title="Settings" subtitle="Preferences, integrations, models, status, and diagnostics." />
      {!isAdmin && (
        <p className="mb-5 rounded-lg bg-(--color-bg-secondary) px-3 py-2 text-sm text-(--color-text-secondary)">
          Admin-only settings are hidden for this account.
        </p>
      )}
      {sections.map((section) => (
        <div key={section.label} className="mb-6">
          <h2 className="mb-2 font-serif text-base font-semibold tracking-tight text-(--color-text-primary)">
            {section.label}
          </h2>
          <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
            {section.items.map((item) => (
              <Card key={item.to} className="p-0" interactive>
                <Link to={item.to} className="flex items-center gap-3 px-3.5 py-3 text-sm">
                  <IconTile hue={item.hue} size={28}>
                    {item.icon}
                  </IconTile>
                  <div className="flex-1">
                    <div className="font-medium text-(--color-text-primary)">{item.label}</div>
                    <div className="text-[11px] text-(--color-text-secondary)">{item.sub}</div>
                  </div>
                  <span className="text-(--color-text-secondary) opacity-50">›</span>
                </Link>
              </Card>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
