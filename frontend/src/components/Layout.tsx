import { useEffect, useRef, useState } from 'react'
import { Link, NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth'
import JobToast from './JobToast'

const linkClass = ({ isActive }: { isActive: boolean }) =>
  `px-3 py-2 rounded-md text-sm font-medium ${
    isActive
      ? 'bg-gray-900 text-white'
      : 'text-gray-300 hover:bg-gray-700 hover:text-white'
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
    <div className="min-h-screen">
      <nav className="bg-gray-800">
        <div className="mx-auto max-w-7xl px-4">
          <div className="flex h-14 items-center justify-between">
            <div className="flex items-center space-x-4">
              <Link to="/" className="text-lg font-bold text-white">
                Harbor Clerk
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
                </>
              )}
            </div>
            <div className="flex items-center space-x-3">
              {isAdmin && (
                <span className="rounded bg-amber-600 px-2 py-0.5 text-xs font-medium text-white">
                  admin
                </span>
              )}
              <div className="relative" ref={menuRef}>
                <button
                  onClick={() => setMenuOpen(!menuOpen)}
                  className="flex items-center space-x-1 rounded-md px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700 hover:text-white"
                >
                  <span>{user?.email}</span>
                  <svg
                    className={`h-4 w-4 transition-transform ${menuOpen ? 'rotate-180' : ''}`}
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={2}
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
                {menuOpen && (
                  <div className="absolute right-0 mt-1 w-48 rounded-md bg-white dark:bg-gray-700 py-1 shadow-lg ring-1 ring-black/5 dark:ring-white/10 z-50">
                    <button
                      onClick={() => {
                        setMenuOpen(false)
                        navigate('/preferences')
                      }}
                      className="block w-full px-4 py-2 text-left text-sm text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-600"
                    >
                      Preferences
                    </button>
                    <div className="border-t border-gray-200 dark:border-gray-600" />
                    <button
                      onClick={() => {
                        setMenuOpen(false)
                        logout()
                      }}
                      className="block w-full px-4 py-2 text-left text-sm text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-600"
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
        <Outlet />
      </main>
      <JobToast />
    </div>
  )
}
