import { useEffect, useRef, useState } from 'react'
import { Link, NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth'
import BackButton from './BackButton'
import { QueueTray } from './queue-tray'

function TabLink({ to, end, children }: { to: string; end?: boolean; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        `relative px-3 py-1.5 text-[13px] font-medium transition-colors ${
          isActive ? 'text-(--color-text-primary)' : 'text-(--color-text-secondary) hover:text-(--color-text-primary)'
        }`
      }
    >
      {({ isActive }) => (
        <>
          {children}
          {isActive && <span className="absolute inset-x-0 -bottom-[7px] h-[2px] bg-(--color-accent) rounded-full" />}
        </>
      )}
    </NavLink>
  )
}

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
    <div className="min-h-screen bg-(--color-bg-secondary)">
      <nav className="sticky top-0 z-40 border-b border-(--color-border) bg-(--bg-vibrancy) backdrop-blur-xl">
        <div className="mx-auto max-w-7xl px-4">
          <div className="flex h-12 items-center justify-between">
            <div className="flex items-center space-x-1">
              <NavLink
                to="/"
                end
                className={({ isActive }) =>
                  `relative mr-2 flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-[13px] font-semibold transition-colors ${
                    isActive
                      ? 'bg-amber-50 dark:bg-amber-900/20 text-amber-700 dark:text-amber-300 ring-1 ring-amber-200/60 dark:ring-amber-700/40'
                      : 'text-(--color-text-secondary) hover:bg-black/4 dark:hover:bg-white/6'
                  }`
                }
              >
                <img src="/favicon.svg" alt="" className="h-6 w-6" />
                <span>Ask</span>
              </NavLink>
              <TabLink to="/upload">Upload</TabLink>
              <TabLink to="/docs">Documents</TabLink>
              <TabLink to="/explore">Explore</TabLink>
              <TabLink to="/search">Search</TabLink>
            </div>
            <div className="flex items-center space-x-1">
              <TabLink to="/stats">Observatory</TabLink>
              {isAdmin && <TabLink to="/admin">System Settings</TabLink>}
              <div className="relative ml-2" ref={menuRef}>
                <button
                  onClick={() => setMenuOpen(!menuOpen)}
                  className="flex items-center space-x-1 rounded-lg px-2.5 py-1.5 text-[13px] text-(--color-text-secondary) hover:bg-black/4 dark:hover:bg-white/6 transition-colors"
                >
                  <div className="flex flex-col items-end">
                    <span>{user?.email}</span>
                    {isAdmin && (
                      <span className="text-[10px] leading-tight text-amber-600 dark:text-amber-400">admin</span>
                    )}
                  </div>
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
                  <div className="absolute right-0 mt-1.5 w-48 rounded-xl bg-(--bg-vibrancy) backdrop-blur-xl py-1 shadow-mac-lg ring-1 ring-(--color-border) z-50">
                    <button
                      onClick={() => {
                        setMenuOpen(false)
                        navigate('/preferences')
                      }}
                      className="block w-full px-3.5 py-2 text-left text-[13px] text-(--color-text-primary) hover:bg-black/4 dark:hover:bg-white/6"
                    >
                      Preferences
                    </button>
                    <div className="mx-3 my-1 border-t border-(--color-border)" />
                    <button
                      onClick={() => {
                        setMenuOpen(false)
                        logout()
                      }}
                      className="block w-full px-3.5 py-2 text-left text-[13px] text-(--color-text-primary) hover:bg-black/4 dark:hover:bg-white/6"
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
      <QueueTray />
    </div>
  )
}
