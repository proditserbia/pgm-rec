import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

export default function Login() {
  const [username, setUsername] = useState('')
  const navigate = useNavigate()
  function enter() {
    localStorage.setItem('pgmrec_authed', '1')
    navigate('/')
  }
  return (
    <div className="login-wrap">
      <div className="login-box">
        <h1>🎬 PGMRec</h1>
        <p className="dev-note">Dev mode — enter any username</p>
        <input
          placeholder="Username"
          value={username}
          onChange={e => setUsername(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && enter()}
          autoFocus
        />
        <button onClick={enter}>Enter</button>
      </div>
    </div>
  )
}
