import { NavLink, useNavigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'

const ROLE_LABELS: Record<string, string> = {
  admin: '👑 Admin',
  export: '📦 Export',
  preview: '👁 Preview',
}

export default function Nav() {
  const navigate = useNavigate()
  const { user, signOut, canExport } = useAuth()

  function logout() {
    signOut()
    navigate('/login')
  }

  return (
    <nav className="nav">
      <NavLink to="/" className="nav-brand"><span className="brand-pgm">PGM</span><span className="brand-rec">Rec</span></NavLink>
      <NavLink to="/" end>Dashboard</NavLink>
      {canExport && <NavLink to="/exports">Export Jobs</NavLink>}
      {canExport && <NavLink to="/exports/new">New Export</NavLink>}
      <span className="nav-user">
        {user ? (
          <>
            <span className="nav-username">{user.username}</span>
            <span className="badge badge-info">{ROLE_LABELS[user.role] ?? user.role}</span>
          </>
        ) : null}
      </span>
      <button className="nav-logout" onClick={logout}>Logout</button>
    </nav>
  )
}
