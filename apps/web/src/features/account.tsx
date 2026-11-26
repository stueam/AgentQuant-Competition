import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, LogIn, UserPlus } from 'lucide-react'
import type { FormEvent } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { api } from '../api'
import type { Identity, Login, Register } from '../api'
import { announceSessionChange, sessionQuery, setSession } from '../session'
import { Feedback, Loading } from '../ui'

export function LoginPage() {
  const session = useQuery(sessionQuery)
  const location = useLocation()
  const navigate = useNavigate()
  const client = useQueryClient()
  const returnTo = validationReturnTo(location.state)

  function destination(identity: Identity) {
    if (identity.role !== 'participant') return '/team'
    if (!identity.team_id) return '/team'
    return returnTo ?? '/'
  }

  const login = useMutation({
    mutationFn: api.login,
    gcTime: 0,
    onSuccess: (identity) => {
      setSession(client, { kind: 'authenticated', identity })
      announceSessionChange()
      navigate(destination(identity), { replace: true, state: { returnTo } })
    },
  })

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    login.mutate(readCredentials(new FormData(event.currentTarget)))
  }

  if (session.isPending) return <Loading>正在读取会话…</Loading>
  if (session.isError) return <Feedback error={session.error} retry={() => void session.refetch()} />
  if (session.data.kind === 'authenticated') {
    return <Navigate to={destination(session.data.identity)} replace state={{ returnTo }} />
  }

  const registered = typeof location.state?.registered === 'string' ? location.state.registered : ''

  return (
    <>
      <header className="page-heading"><h1>账号登录</h1></header>
      <section className="panel identity-panel">
        <div className="panel-heading">
          <span className="section-icon"><LogIn size={20} /></span>
          <h2>登录 AgentQuant</h2>
        </div>
        {registered && <p className="success-message" role="status">账号已注册，请登录。</p>}
        {session.data.kind === 'expired' && <p className="muted">会话已失效，请重新登录。</p>}
        <form onSubmit={submit} className="submission-form">
          <CredentialsFields username={registered} />
          {login.isError && <Feedback error={login.error} />}
          <button type="submit" className="button primary" disabled={login.isPending}>
            {login.isPending ? '正在登录…' : '登录'}<ArrowRight size={16} aria-hidden="true" />
          </button>
        </form>
        <div className="identity-actions"><Link className="text-link" to="/register" state={{ returnTo }}>使用邀请码注册</Link></div>
      </section>
    </>
  )
}

export function RegisterPage() {
  const session = useQuery(sessionQuery)
  const location = useLocation()
  const navigate = useNavigate()
  const returnTo = validationReturnTo(location.state)
  const register = useMutation({
    mutationFn: api.register,
    gcTime: 0,
    onSuccess: (_, { username }) => navigate('/login', { replace: true, state: { registered: username, returnTo } }),
  })

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const fields = new FormData(event.currentTarget)
    const body: Register = { ...readCredentials(fields), invite_code: String(fields.get('invite_code')).trim() }
    register.mutate(body)
  }

  if (session.isPending) return <Loading>正在读取会话…</Loading>
  if (session.isError) return <Feedback error={session.error} retry={() => void session.refetch()} />
  if (session.data.kind === 'authenticated') return <Navigate to="/team" replace />

  return (
    <>
      <header className="page-heading"><h1>邀请码注册</h1></header>
      <section className="panel identity-panel">
        <div className="panel-heading">
          <span className="section-icon"><UserPlus size={20} /></span>
          <div><h2>注册参赛账号</h2><p>使用组织者签发的一次性注册邀请码。</p></div>
        </div>
        <form onSubmit={submit} className="submission-form">
          <CredentialsFields registration />
          <label htmlFor="registration-invite">注册邀请码</label>
          <input id="registration-invite" name="invite_code" required minLength={32} maxLength={128}
            autoComplete="off" spellCheck={false} />
          {register.isError && <Feedback error={register.error} />}
          <button type="submit" className="button primary" disabled={register.isPending}>
            {register.isPending ? '正在注册…' : '注册'}
          </button>
        </form>
        <div className="identity-actions"><Link className="text-link" to="/login" state={{ returnTo }}>返回登录</Link></div>
      </section>
    </>
  )
}

function CredentialsFields({ username, registration }: { username?: string; registration?: boolean }) {
  return (
    <>
      <label htmlFor="account-username">用户名</label>
      <input id="account-username" name="username" autoComplete="username" autoCapitalize="none" spellCheck={false}
        required minLength={3} maxLength={40} pattern={String.raw`[A-Za-z0-9][A-Za-z0-9_.\-]{2,39}`} defaultValue={username}
        aria-describedby={registration ? 'username-rule' : undefined} />
      {registration && <small className="muted" id="username-rule">3–40 个字符，以字母或数字开头，可包含点、连字符和下划线。</small>}
      <label htmlFor="account-password">密码</label>
      <input id="account-password" name="password" type="password" autoComplete={registration ? 'new-password' : 'current-password'}
        required minLength={12} maxLength={128} aria-describedby={registration ? 'password-rule' : undefined} />
      {registration && <small className="muted" id="password-rule">至少 12 个字符。</small>}
    </>
  )
}

function readCredentials(fields: FormData): Login {
  return { username: String(fields.get('username')).trim().toLowerCase(), password: String(fields.get('password')) }
}

export function validationReturnTo(state: unknown) {
  if (typeof state !== 'object' || !state || !('returnTo' in state)) return
  const route = state.returnTo
  if (typeof route === 'string' && (route === '/' || route.startsWith('/runs/'))) return route
}
