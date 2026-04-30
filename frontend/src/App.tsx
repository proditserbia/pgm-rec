import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider, useAuth } from './contexts/AuthContext'
import Nav from './components/Nav'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import ChannelDetail from './pages/ChannelDetail'
import ExportCreate from './pages/ExportCreate'
import ExportJobs from './pages/ExportJobs'

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth()
  if (loading) return <div className="loading-wrap">Loading…</div>
  if (!user) return <Navigate to="/login" replace />
  return <>{children}</>
}

function Layout({ children }: { children: React.ReactNode }) {
  return (
    <RequireAuth>
      <Nav />
      {children}
    </RequireAuth>
  )
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/" element={<Layout><Dashboard /></Layout>} />
      <Route path="/channels/:id" element={<Layout><ChannelDetail /></Layout>} />
      <Route path="/exports/new" element={<Layout><ExportCreate /></Layout>} />
      <Route path="/exports" element={<Layout><ExportJobs /></Layout>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  )
}
