import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth'
import BackButton from './BackButton'
import { LLMStatusBanner, LLMStatusProvider } from './LLMStatusBanner'
import { QueueTray } from './queue-tray'
import CorpusEmptyBanner from './CorpusEmptyBanner'
import OnboardingWizard from './OnboardingWizard'
import { useCorpusBannerState } from '../hooks/useCorpusBannerState'
import { useAreaAccent } from '../hooks/useAreaAccent'

function TabLink({ to, end, icon, children }: { to: string; end?: boolean; icon?: string; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        `relative inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] font-medium transition-colors ${
          isActive
            ? 'text-(--area-accent-text)'
            : 'text-(--color-text-secondary) hover:bg-black/4 dark:hover:bg-white/6 hover:text-(--color-text-primary)'
        }`
      }
      style={({ isActive }) =>
        isActive
          ? {
              backgroundColor: 'var(--area-accent-tint)',
              boxShadow: 'inset 0 0 0 1px var(--area-accent)',
            }
          : undefined
      }
    >
      {icon && <span aria-hidden="true">{icon}</span>}
      <span>{children}</span>
    </NavLink>
  )
}

export default function Layout() {
  const { user, logout, isAdmin, updatePreferences } = useAuth()
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()
  const location = useLocation()
  const bannerState = useCorpusBannerState()
  useAreaAccent()
  const bannerSuppressedPath =
    location.pathname === '/folders' ||
    location.pathname.startsWith('/admin') ||
    location.pathname.startsWith('/preferences')
  const showBanner = bannerState !== null && !bannerSuppressedPath
  const showWizard = !!user && !user.preferences?.onboardingComplete

  async function handleOnboardingComplete() {
    try {
      await updatePreferences({ onboardingComplete: true })
    } catch {
      // Best-effort persistence; wizard closes locally regardless.
    }
  }

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
    <div data-layout-root className="min-h-screen bg-(--color-bg-primary)">
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
              <TabLink to="/research" icon="🐙">
                Research
              </TabLink>
              <TabLink to="/folders" icon="📁">
                Folders
              </TabLink>
              <TabLink to="/docs" icon="📄">
                Documents
              </TabLink>
              <TabLink to="/explore" icon="🌍">
                Explore
              </TabLink>
              <TabLink to="/search" icon="🔍">
                Search
              </TabLink>
            </div>
            <div className="flex items-center space-x-1">
              <TabLink to="/stats" icon="📊">
                Observatory
              </TabLink>
              {isAdmin && (
                <TabLink to="/integrations" icon="🔌">
                  Integrations
                </TabLink>
              )}
              {isAdmin && (
                <TabLink to="/admin" icon="⚙️">
                  System Settings
                </TabLink>
              )}
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
      <LLMStatusProvider>
        <main className="mx-auto max-w-7xl px-4 py-6">
          {showBanner && bannerState && <CorpusEmptyBanner state={bannerState} />}
          <BackButton />
          <Outlet />
        </main>
        <QueueTray />
        <LLMStatusBanner />
        {showWizard && <OnboardingWizard onComplete={handleOnboardingComplete} />}
      </LLMStatusProvider>
    </div>
  )
}
