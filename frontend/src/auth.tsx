import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { configureApi, patch, post } from './api'

interface User {
  user_id: string
  email: string
  role: string
  preferences: { theme?: string; page_size?: number }
}

interface AuthState {
  user: User | null
  token: string | null
  loading: boolean
  needsSetup: boolean
  login: (email: string, password: string) => Promise<void>
  logout: () => Promise<void>
  updatePreferences: (prefs: Partial<User['preferences']>) => Promise<void>
  isAdmin: boolean
}

const AuthContext = createContext<AuthState | null>(null)

function applyTheme(theme?: string) {
  if (theme === 'dark') {
    document.documentElement.classList.add('dark')
  } else {
    document.documentElement.classList.remove('dark')
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [token, setToken] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [needsSetup, setNeedsSetup] = useState(false)
  const tokenRef = useRef<string | null>(null)

  // Keep ref in sync
  tokenRef.current = token

  const clearAuth = useCallback(() => {
    setToken(null)
    setUser(null)
    tokenRef.current = null
  }, [])

  // Configure API layer on mount
  useEffect(() => {
    configureApi(
      () => tokenRef.current,
      (t) => {
        tokenRef.current = t
        setToken(t)
      },
      clearAuth,
    )
  }, [clearAuth])

  // Check setup status + attempt silent refresh on mount
  useEffect(() => {
    let cancelled = false
    async function init() {
      try {
        // Check if initial setup is needed
        const setupRes = await fetch('/api/system/setup-status')
        if (setupRes.ok && !cancelled) {
          const setupData = await setupRes.json()
          if (setupData.needs_setup) {
            setNeedsSetup(true)
            return
          }
        }
        // Attempt silent refresh
        const res = await fetch('/api/auth/refresh', {
          method: 'POST',
          credentials: 'include',
        })
        if (res.ok && !cancelled) {
          const data = await res.json()
          setToken(data.access_token)
          tokenRef.current = data.access_token
          // Fetch user info
          const meRes = await fetch('/api/me', {
            headers: { Authorization: `Bearer ${data.access_token}` },
          })
          if (meRes.ok && !cancelled) {
            const userData: User = await meRes.json()
            setUser(userData)
            applyTheme(userData.preferences?.theme)
          }
        }
      } catch {
        // No refresh token — stay logged out
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    init()
    return () => {
      cancelled = true
    }
  }, [])

  const login = useCallback(async (email: string, password: string) => {
    const data: { access_token: string; user: User } = await post('/api/auth/login', { email, password })
    tokenRef.current = data.access_token
    setToken(data.access_token)
    setUser(data.user)
    applyTheme(data.user.preferences?.theme)
  }, [])

  const logout = useCallback(async () => {
    try {
      await post('/api/auth/logout')
    } catch {
      // ignore
    }
    clearAuth()
    applyTheme('light')
  }, [clearAuth])

  const updatePreferences = useCallback(async (prefs: Partial<User['preferences']>) => {
    const updated: User = await patch('/api/me/preferences', prefs)
    setUser(updated)
    applyTheme(updated.preferences?.theme)
  }, [])

  return (
    <AuthContext.Provider
      value={{
        user,
        token,
        loading,
        needsSetup,
        login,
        logout,
        updatePreferences,
        isAdmin: user?.role === 'admin',
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
