import { HTMLAttributes, forwardRef } from 'react'

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** Apply the hover treatment (border-color shift). Use for clickable list rows; omit for static informational containers. */
  interactive?: boolean
}

/**
 * Surface card with the standard gradient + hairline border treatment.
 * Wraps the .surface-card layer-class defined in index.css. Forwarded ref
 * for callers that need it (e.g. focus management).
 *
 * Usage: <Card className="p-4">…</Card>
 * Usage (clickable row): <Card interactive className="p-4">…</Card>
 *
 * The .surface-card class supplies bg, gradient, and border.
 * The .surface-card-interactive class adds the hover border-color shift.
 * Padding, layout, and per-instance overrides come from className as usual.
 */
export const Card = forwardRef<HTMLDivElement, CardProps>(function Card(
  { className = '', interactive = false, children, ...rest },
  ref,
) {
  const cls = `surface-card ${interactive ? 'surface-card-interactive' : ''} ${className}`.trim()
  return (
    <div ref={ref} className={cls} {...rest}>
      {children}
    </div>
  )
})
