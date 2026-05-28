import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider, useAuth } from './auth/AuthContext'
import Sidebar from './components/Sidebar'
import Header from './components/Header'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Approvals from './pages/Approvals'
import Plugins from './pages/Plugins'
import Memory from './pages/Memory'
import KnowledgeGraph from './pages/KnowledgeGraph'
import Activity from './pages/Activity'
import Findings from './pages/Findings'
import Chat from './pages/Chat'
import Specialists from './pages/Specialists'
import Settings from './pages/Settings'

function AppShell() {
  const { user, loading, logout } = useAuth()

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{ background: '#f3f1ee' }}>
        <div className="text-sm" style={{ color: '#858481' }}>Loading…</div>
      </div>
    )
  }

  if (!user) {
    return (
      <Routes>
        <Route path="*" element={<Login />} />
      </Routes>
    )
  }

  return (
    <div className="flex h-screen overflow-hidden" style={{ background: '#f3f1ee' }}>
      <Sidebar />
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
        <Header user={user} onLogout={logout} />
        <main className="flex-1 overflow-auto">
          <Routes>
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/approvals" element={<Approvals />} />
            <Route path="/plugins" element={<Plugins />} />
            <Route path="/memory" element={<Memory />} />
            <Route path="/knowledge-graph" element={<KnowledgeGraph />} />
            <Route path="/activity" element={<Activity />} />
            <Route path="/findings" element={<Findings />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/specialists" element={<Specialists />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AppShell />
      </AuthProvider>
    </BrowserRouter>
  )
}
