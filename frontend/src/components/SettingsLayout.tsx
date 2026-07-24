import { NavLink, Outlet } from 'react-router'
import { useAuth } from '../auth'
import { settingsSectionsForRole, type SettingsItem } from '../settingsNav'
import { IconTile } from './IconTile'

function SettingsNavLink({ item }: { item: SettingsItem }) {
  return (
    <NavLink
      to={item.to}
      end={item.to === '/settings'}
      className={({ isActive }) =>
        `group flex min-w-[11rem] items-center gap-2 rounded-lg px-2 py-1.5 text-sm transition-colors lg:min-w-0 ${
          isActive
            ? 'bg-(--area-accent-tint) text-(--area-accent-text) ring-1 ring-(--area-accent)'
            : 'text-(--color-text-secondary) hover:bg-black/4 hover:text-(--color-text-primary) dark:hover:bg-white/6'
        }`
      }
    >
      <IconTile hue={item.hue} size={24}>
        {item.icon}
      </IconTile>
      <span className="truncate">{item.label}</span>
    </NavLink>
  )
}

export default function SettingsLayout() {
  const { isAdmin } = useAuth()
  const sections = settingsSectionsForRole(isAdmin)

  return (
    <div className="grid gap-6 lg:grid-cols-[230px_minmax(0,1fr)]">
      <aside className="border-b border-(--color-border) pb-4 lg:border-b-0 lg:border-r lg:pb-0 lg:pr-4">
        <div className="lg:sticky lg:top-16">
          <div className="mb-3 flex items-baseline justify-between gap-3 lg:block">
            <h2 className="text-sm font-semibold text-(--color-text-primary)">Settings</h2>
            <p className="text-xs text-(--color-text-secondary)">Controls and diagnostics</p>
          </div>
          <div className="flex gap-4 overflow-x-auto pb-1 lg:block lg:space-y-4 lg:overflow-visible lg:pb-0">
            {sections.map((section) => (
              <div key={section.label} className="shrink-0 lg:shrink">
                <div className="mb-1.5 px-2 text-[11px] font-semibold uppercase tracking-wide text-(--color-text-secondary)">
                  {section.label}
                </div>
                <nav className="flex gap-1 lg:block lg:space-y-1" aria-label={`${section.label} settings`}>
                  {section.items.map((item) => (
                    <SettingsNavLink key={item.to} item={item} />
                  ))}
                </nav>
              </div>
            ))}
          </div>
        </div>
      </aside>
      <section className="min-w-0">
        <Outlet />
      </section>
    </div>
  )
}
