import { ReactNode } from 'react'

interface PageHeaderProps {
  title: ReactNode
  subtitle?: ReactNode
  /** Optional content rendered to the right of the title (action button, search input, etc.). */
  actions?: ReactNode
}

/**
 * Standard page-title block: serif title + short accent bar tinted with
 * the active area's hue + optional subtitle line + optional right-aligned
 * actions slot.
 *
 * The accent bar uses var(--area-accent) which is bound by the
 * useAreaAccent hook on the layout root — so every page that renders
 * inside Layout automatically gets the right color.
 */
export function PageHeader({ title, subtitle, actions }: PageHeaderProps) {
  return (
    <div className="mb-4 flex items-start justify-between gap-4">
      <div>
        <h1 className="font-serif text-3xl font-semibold tracking-tight text-(--color-text-primary)">{title}</h1>
        <div className="mt-1.5 h-[3px] w-12 rounded-sm bg-(--area-accent)" />
        {subtitle && <p className="mt-2 text-sm text-(--color-text-secondary)">{subtitle}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  )
}
