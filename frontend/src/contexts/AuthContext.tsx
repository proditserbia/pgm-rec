import { createContext, useContext, useState, useEffect, useCallback } from 'react'
import type { ReactNode } from 'react'
import type { UserResponse } from '../types'
import { getCurrentUser, getToken, setToken, clearToken } from '../api/client'

interface AuthState {
  user: UserResponse | null
  token: string | null
  loading: boolean
}

interface AuthContextValue extends AuthState {
  signIn: (token: string, user: UserResponse) => void
  signOut: () => void
  isAdmin: boolean
  canExport: boolean
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({
    user: null,
    token: getToken(),
    loading: true,
  })

  // On mount: if there's a stored token, fetch /me to validate it
  useEffect(() => {
    const stored = getToken()
    if (!stored) {
      setState({ user: null, token: null, loading: false })
      return
    }
    getCurrentUser()
      .then(user => setState({ user, token: stored, loading: false }))
      .catch(() => {
        clearToken()
        setState({ user: null, token: null, loading: false })
      })
  }, [])

  const signIn = useCallback((token: string, user: UserResponse) => {
    setToken(token)
    setState({ user, token, loading: false })
  }, [])

  const signOut = useCallback(() => {
    clearToken()
    setState({ user: null, token: null, loading: false })
  }, [])

  const isAdmin = state.user?.role === 'admin'
  const canExport = state.user?.role === 'admin' || state.user?.role === 'export'

  return (
    <AuthContext.Provider value={{ ...state, signIn, signOut, isAdmin, canExport }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
