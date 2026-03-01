import { Link } from 'react-router-dom'

const ITEMS = [
  { to: '/admin/users', label: 'Users', description: 'Manage user accounts and roles' },
  { to: '/admin/keys', label: 'API Keys', description: 'Create and revoke API keys' },
  { to: '/admin/models', label: 'Models', description: 'Download and manage LLM models' },
  { to: '/admin/retrieval', label: 'Retrieval', description: 'Chat and MCP search behavior' },
  { to: '/admin/system/status', label: 'System Status', description: 'Health checks and statistics' },
  { to: '/admin/system/logs', label: 'Service Logs', description: 'View log files and tail commands' },
  { to: '/admin/system/maintenance', label: 'System Maintenance', description: 'Purge, reaper, and cleanup' },
]

export default function SystemSettingsPage() {
  return (
    <div>
      <h1 className="mb-4 text-xl font-bold">System Settings</h1>
      <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac overflow-hidden divide-y divide-(--color-border)">
        {ITEMS.map((item) => (
          <Link
            key={item.to}
            to={item.to}
            className="flex items-center justify-between px-4 py-3.5 hover:bg-black/2 dark:hover:bg-white/2 transition-colors"
          >
            <div>
              <div className="text-[14px] font-medium text-(--color-text-primary)">{item.label}</div>
              <div className="text-[12px] text-(--color-text-secondary) mt-0.5">{item.description}</div>
            </div>
            <svg
              className="h-4 w-4 text-gray-300 dark:text-gray-600 shrink-0"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
            </svg>
          </Link>
        ))}
      </div>
    </div>
  )
}
