import { Link } from 'react-router'
import { useAuth } from '../auth'
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'
import { IconTile } from '../components/IconTile'
import { settingsSectionsForRole } from '../settingsNav'

export default function SystemSettingsPage() {
  const { isAdmin } = useAuth()
  const sections = settingsSectionsForRole(isAdmin)

  return (
    <div className="mx-auto max-w-4xl px-4 py-8">
      <PageHeader
        title="Settings"
        subtitle={
          isAdmin ? 'Preferences, integrations, models, status, and diagnostics.' : 'Preferences and account settings.'
        }
      />
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
