import { NavLink, useNavigate } from 'react-router-dom'

export default function Nav() {
  const navigate = useNavigate()
  function logout() {
    localStorage.removeItem('pgmrec_authed')
    navigate('/login')
  }
  return (
    <nav className="nav">
      <NavLink to="/" className="nav-brand">🎬 PGMRec</NavLink>
      <NavLink to="/" end>Dashboard</NavLink>
      <NavLink to="/exports">Export Jobs</NavLink>
      <NavLink to="/exports/new">New Export</NavLink>
      <button className="nav-logout" onClick={logout}>Logout</button>
    </nav>
  )
}
