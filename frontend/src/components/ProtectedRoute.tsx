import { Navigate, Outlet } from 'react-router'
import { useAuth } from '../auth'

export default function ProtectedRoute() {
  const { user, loading, needsSetup } = useAuth()

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="text-gray-500 dark:text-gray-400">Loading...</div>
      </div>
    )
  }

  if (needsSetup) {
    return <Navigate to="/setup" replace />
  }

  if (!user) {
    return <Navigate to="/login" replace />
  }

  return <Outlet />
}
