import { Route, Routes, Navigate, useParams } from 'react-router-dom'
import Layout from './components/Layout'
import ProtectedRoute from './components/ProtectedRoute'
import AdminRoute from './components/AdminRoute'
import LoginPage from './pages/LoginPage'
import DocumentsPage from './pages/DocumentsPage'
import DocumentDetailPage from './pages/DocumentDetailPage'
import UploadPage from './pages/UploadPage'
import SearchPage from './pages/SearchPage'
import UsersPage from './pages/UsersPage'
import ApiKeysPage from './pages/ApiKeysPage'
import SetupPage from './pages/SetupPage'
import SystemStatusPage from './pages/SystemStatusPage'
import SystemMaintenancePage from './pages/SystemMaintenancePage'
import ChatPage from './pages/ChatPage'
import HomePage from './pages/HomePage'
import ModelsPage from './pages/ModelsPage'
import PreferencesPage from './pages/PreferencesPage'
import SystemSettingsPage from './pages/SystemSettingsPage'

function ChatRedirect() {
  const { conversationId } = useParams<{ conversationId: string }>()
  return <Navigate to={`/c/${conversationId}`} replace />
}

export default function App() {
  return (
    <Routes>
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/login" element={<LoginPage />} />
      <Route element={<ProtectedRoute />}>
        <Route element={<Layout />}>
          <Route path="/" element={<HomePage />} />
          <Route path="/c/:conversationId" element={<ChatPage />} />
          <Route path="/upload" element={<UploadPage />} />
          <Route path="/docs" element={<DocumentsPage />} />
          <Route path="/docs/:id" element={<DocumentDetailPage />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/chat" element={<Navigate to="/" replace />} />
          <Route path="/chat/:conversationId" element={<ChatRedirect />} />
          <Route path="/preferences" element={<PreferencesPage />} />
          <Route element={<AdminRoute />}>
            <Route path="/admin" element={<SystemSettingsPage />} />
            <Route path="/admin/users" element={<UsersPage />} />
            <Route path="/admin/keys" element={<ApiKeysPage />} />
            <Route path="/admin/system" element={<Navigate to="/admin/system/status" replace />} />
            <Route path="/admin/system/status" element={<SystemStatusPage />} />
            <Route path="/admin/system/maintenance" element={<SystemMaintenancePage />} />
            <Route path="/admin/models" element={<ModelsPage />} />
          </Route>
        </Route>
      </Route>
    </Routes>
  )
}
