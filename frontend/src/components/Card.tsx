import { HTMLAttributes, forwardRef } from 'react'

type CardProps = HTMLAttributes<HTMLDivElement>

/**
 * Surface card with the standard gradient + hairline border treatment.
 * Wraps the .surface-card layer-class defined in index.css. Forwarded ref
 * for callers that need it (e.g. focus management).
 *
 * Usage: <Card className="p-4">…</Card>
 *
 * The .surface-card class supplies bg, gradient, border, and hover state.
 * Padding, layout, and per-instance overrides come from className as usual.
 */
export const Card = forwardRef<HTMLDivElement, CardProps>(function Card({ className = '', children, ...rest }, ref) {
  return (
    <div ref={ref} className={`surface-card ${className}`} {...rest}>
      {children}
    </div>
  )
})
