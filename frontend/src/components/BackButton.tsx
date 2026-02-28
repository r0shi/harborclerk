import { useLocation, useNavigate } from 'react-router-dom'

const TOP_LEVEL = new Set([
  '/',
  '/upload',
  '/docs',
  '/search',
  '/admin',
  '/preferences',
  '/login',
  '/setup',
])

function isTopLevel(pathname: string): boolean {
  if (TOP_LEVEL.has(pathname)) return true
  if (pathname.startsWith('/c/')) return true
  return false
}

export default function BackButton() {
  const location = useLocation()
  const navigate = useNavigate()

  if (isTopLevel(location.pathname)) return null

  return (
    <button
      onClick={() => navigate(-1)}
      className="mb-4 inline-flex items-center gap-0.5 text-[14px] font-medium text-[var(--color-accent)] hover:opacity-70 transition-opacity"
    >
      <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5L8.25 12l7.5-7.5" />
      </svg>
      Back
    </button>
  )
}
