import { Navigate, Outlet } from 'react-router'
import { useAuth } from '../auth'

export default function AdminRoute() {
  const { isAdmin } = useAuth()

  if (!isAdmin) {
    return <Navigate to="/" replace />
  }

  return <Outlet />
}
