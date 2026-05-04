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
    label: 'Access & identity',
    items: [
      { to: '/admin/users', label: 'Users', sub: 'Manage accounts and roles', icon: '👥', hue: 'docs' },
      { to: '/admin/keys', label: 'API Keys', sub: 'Create and revoke API keys', icon: '🔑', hue: 'research' },
    ],
  },
  {
    label: 'Models & languages',
    items: [
      { to: '/admin/models', label: 'Models', sub: 'Download and manage LLM models', icon: '🧠', hue: 'observatory' },
      { to: '/admin/languages', label: 'Languages', sub: 'OCR & entity language packs', icon: '🌐', hue: 'explore' },
    ],
  },
  {
    label: 'Behavior & limits',
    items: [
      { to: '/admin/retrieval', label: 'Retrieval', sub: 'Chat & MCP search behavior', icon: '🔍', hue: 'search' },
      { to: '/admin/rate-limits', label: 'Rate Limits', sub: 'Default API key rate limits', icon: '⏱', hue: 'ask' },
    ],
  },
  {
    label: 'Operations',
    items: [
      {
        to: '/admin/system/status',
        label: 'System Status',
        sub: 'Health checks and statistics',
        icon: '💚',
        hue: 'observatory',
      },
      {
        to: '/admin/system/logs',
        label: 'Service Logs',
        sub: 'View log files and tail commands',
        icon: '📜',
        hue: 'settings',
      },
      {
        to: '/admin/system/maintenance',
        label: 'System Maintenance',
        sub: 'Purge, reaper, and cleanup',
        icon: '🧹',
        hue: 'ask',
      },
    ],
  },
]

export default function SystemSettingsPage() {
  const { user } = useAuth()
  if (user?.role !== 'admin') {
    return (
      <div className="mx-auto max-w-4xl px-4 py-8">
        <PageHeader title="System Settings" />
        <p className="text-sm text-(--color-text-secondary)">Admins only.</p>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-4xl px-4 py-8">
      <PageHeader title="System Settings" />
      {SECTIONS.map((section) => (
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
