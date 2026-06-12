import { Route, Routes, Navigate, useParams } from 'react-router-dom'
import Layout from './components/Layout'
import ProtectedRoute from './components/ProtectedRoute'
import AdminRoute from './components/AdminRoute'
import SettingsLayout from './components/SettingsLayout'
import LoginPage from './pages/LoginPage'
import DocumentsPage from './pages/DocumentsPage'
import DocumentDetailPage from './pages/DocumentDetailPage'
import FoldersPage from './pages/FoldersPage'
import SearchPage from './pages/SearchPage'
import UsersPage from './pages/UsersPage'
import ApiKeysPage from './pages/ApiKeysPage'
import ApiKeyDashboardPage from './pages/ApiKeyDashboardPage'
import SetupPage from './pages/SetupPage'
import SystemStatusPage from './pages/SystemStatusPage'
import SystemMaintenancePage from './pages/SystemMaintenancePage'
import ServiceLogsPage from './pages/ServiceLogsPage'
import ChatPage from './pages/ChatPage'
import HomePage from './pages/HomePage'
import LanguagesPage from './pages/LanguagesPage'
import ModelsPage from './pages/ModelsPage'
import PreferencesPage from './pages/PreferencesPage'
import RateLimitSettingsPage from './pages/RateLimitSettingsPage'
import RetrievalSettingsPage from './pages/RetrievalSettingsPage'
import StatsPage from './pages/StatsPage'
import ExplorePage from './pages/ExplorePage'
import ResearchPage from './pages/ResearchPage'
import SystemSettingsPage from './pages/SystemSettingsPage'
import IntegrationsPage from './pages/IntegrationsPage'
import SecuritySettingsPage from './pages/SecuritySettingsPage'

function ChatRedirect() {
  const { conversationId } = useParams<{ conversationId: string }>()
  return <Navigate to={`/c/${conversationId}`} replace />
}

function ApiKeyRedirect() {
  const { keyId } = useParams<{ keyId: string }>()
  return <Navigate to={`/settings/api-keys/${keyId}`} replace />
}

export default function App() {
  return (
    <Routes>
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/login" element={<LoginPage />} />
      <Route element={<ProtectedRoute />}>
        <Route element={<Layout />}>
          <Route path="/" element={<HomePage />} />
          <Route path="/ask" element={<ChatPage />} />
          <Route path="/c/:conversationId" element={<ChatPage />} />
          <Route path="/folders" element={<FoldersPage />} />
          <Route path="/docs" element={<DocumentsPage />} />
          <Route path="/docs/:id" element={<DocumentDetailPage />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/explore" element={<ExplorePage />} />
          <Route path="/research/:researchId?" element={<ResearchPage />} />
          <Route path="/stats" element={<StatsPage />} />
          <Route path="/chat" element={<Navigate to="/ask" replace />} />
          <Route path="/chat/:conversationId" element={<ChatRedirect />} />
          <Route path="/preferences" element={<Navigate to="/settings/preferences" replace />} />
          <Route path="/settings" element={<SettingsLayout />}>
            <Route index element={<SystemSettingsPage />} />
            <Route path="preferences" element={<PreferencesPage />} />
            <Route element={<AdminRoute />}>
              <Route path="integrations" element={<IntegrationsPage />} />
              <Route path="users" element={<UsersPage />} />
              <Route path="api-keys" element={<ApiKeysPage />} />
              <Route path="api-keys/:keyId" element={<ApiKeyDashboardPage />} />
              <Route path="models" element={<ModelsPage />} />
              <Route path="languages" element={<LanguagesPage />} />
              <Route path="retrieval" element={<RetrievalSettingsPage />} />
              <Route path="rate-limits" element={<RateLimitSettingsPage />} />
              <Route path="status" element={<SystemStatusPage />} />
              <Route path="diagnostics" element={<ServiceLogsPage />} />
              <Route path="maintenance" element={<SystemMaintenancePage />} />
              <Route path="security" element={<SecuritySettingsPage />} />
            </Route>
          </Route>
          <Route element={<AdminRoute />}>
            <Route path="/integrations" element={<Navigate to="/settings/integrations" replace />} />
            <Route path="/admin" element={<Navigate to="/settings" replace />} />
            <Route path="/admin/users" element={<Navigate to="/settings/users" replace />} />
            <Route path="/admin/keys" element={<Navigate to="/settings/api-keys" replace />} />
            <Route path="/admin/keys/:keyId" element={<ApiKeyRedirect />} />
            <Route path="/admin/system" element={<Navigate to="/settings/status" replace />} />
            <Route path="/admin/system/status" element={<Navigate to="/settings/status" replace />} />
            <Route path="/admin/system/logs" element={<Navigate to="/settings/diagnostics" replace />} />
            <Route path="/admin/system/maintenance" element={<Navigate to="/settings/maintenance" replace />} />
            <Route path="/admin/system/security" element={<Navigate to="/settings/security" replace />} />
            <Route path="/admin/models" element={<Navigate to="/settings/models" replace />} />
            <Route path="/admin/languages" element={<Navigate to="/settings/languages" replace />} />
            <Route path="/admin/retrieval" element={<Navigate to="/settings/retrieval" replace />} />
            <Route path="/admin/rate-limits" element={<Navigate to="/settings/rate-limits" replace />} />
          </Route>
        </Route>
      </Route>
    </Routes>
  )
}
