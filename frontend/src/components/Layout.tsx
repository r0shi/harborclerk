import { useEffect, useRef, useState } from 'react'
import { Link, NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth'
import BackButton from './BackButton'
import JobToast from './JobToast'

const linkClass = ({ isActive }: { isActive: boolean }) =>
  `px-3 py-1.5 rounded-lg text-[13px] font-medium transition-colors ${
    isActive
      ? 'text-[var(--color-accent)]'
      : 'text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)]'
  }`

export default function Layout() {
  const { user, logout, isAdmin } = useAuth()
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false)
      }
    }
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setMenuOpen(false)
    }
    if (menuOpen) {
      document.addEventListener('mousedown', handleClick)
      document.addEventListener('keydown', handleKey)
    }
    return () => {
      document.removeEventListener('mousedown', handleClick)
      document.removeEventListener('keydown', handleKey)
    }
  }, [menuOpen])

  return (
    <div className="min-h-screen bg-[var(--color-bg-secondary)]">
      <nav className="sticky top-0 z-40 border-b border-[var(--color-border)] bg-[var(--bg-vibrancy)] backdrop-blur-xl">
        <div className="mx-auto max-w-7xl px-4">
          <div className="flex h-12 items-center justify-between">
            <div className="flex items-center space-x-1">
              <Link to="/" className="mr-3 flex items-center space-x-2 text-[15px] font-semibold text-[var(--color-text-primary)]">
                <img src="/favicon.png" alt="" className="h-5 w-5" />
                <span>Harbor Clerk</span>
              </Link>
              <NavLink to="/" end className={linkClass}>
                Upload
              </NavLink>
              <NavLink to="/docs" className={linkClass}>
                Documents
              </NavLink>
              <NavLink to="/search" className={linkClass}>
                Search
              </NavLink>
              <NavLink to="/chat" className={linkClass}>
                Chat
              </NavLink>
              {isAdmin && (
                <>
                  <NavLink to="/admin/users" className={linkClass}>
                    Users
                  </NavLink>
                  <NavLink to="/admin/keys" className={linkClass}>
                    API Keys
                  </NavLink>
                  <NavLink to="/admin/system" className={linkClass}>
                    System
                  </NavLink>
                  <NavLink to="/admin/models" className={linkClass}>
                    Models
                  </NavLink>
                </>
              )}
            </div>
            <div className="flex items-center space-x-3">
              {isAdmin && (
                <span className="rounded-md bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-600 dark:text-amber-400">
                  admin
                </span>
              )}
              <div className="relative" ref={menuRef}>
                <button
                  onClick={() => setMenuOpen(!menuOpen)}
                  className="flex items-center space-x-1 rounded-lg px-2.5 py-1.5 text-[13px] text-[var(--color-text-secondary)] hover:bg-black/[0.04] dark:hover:bg-white/[0.06] transition-colors"
                >
                  <span>{user?.email}</span>
                  <svg
                    className={`h-3.5 w-3.5 transition-transform ${menuOpen ? 'rotate-180' : ''}`}
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={2}
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
                {menuOpen && (
                  <div className="absolute right-0 mt-1.5 w-48 rounded-xl bg-[var(--bg-vibrancy)] backdrop-blur-xl py-1 shadow-mac-lg ring-1 ring-[var(--color-border)] z-50">
                    <button
                      onClick={() => {
                        setMenuOpen(false)
                        navigate('/preferences')
                      }}
                      className="block w-full px-3.5 py-2 text-left text-[13px] text-[var(--color-text-primary)] hover:bg-black/[0.04] dark:hover:bg-white/[0.06]"
                    >
                      Preferences
                    </button>
                    <div className="mx-3 my-1 border-t border-[var(--color-border)]" />
                    <button
                      onClick={() => {
                        setMenuOpen(false)
                        logout()
                      }}
                      className="block w-full px-3.5 py-2 text-left text-[13px] text-[var(--color-text-primary)] hover:bg-black/[0.04] dark:hover:bg-white/[0.06]"
                    >
                      Logout
                    </button>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      </nav>
      <main className="mx-auto max-w-7xl px-4 py-6">
        <BackButton />
        <Outlet />
      </main>
      <JobToast />
    </div>
  )
}
