import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { Link, Outlet, useLocation } from 'react-router-dom'
import { ApiError } from '../api'
import { submissionsQuery } from '../queries'
import { sessionQuery } from '../session'
import type { ValidationAccess } from '../session'
import { Feedback, Loading } from '../ui'

export function ValidationGate() {
  const session = useQuery(sessionQuery)
  const demo = useQuery({ ...submissionsQuery('demo'), enabled: session.data?.kind === 'anonymous' })
  const { pathname: returnTo } = useLocation()

  if (session.isPending) return <Loading>正在读取会话…</Loading>
  if (session.isError) return <Feedback error={session.error} retry={() => void session.refetch()} />

  if (session.data.kind === 'authenticated') {
    const { id, role, team_id, csrf_token } = session.data.identity
    if (role !== 'participant') {
      return <AccessNotice title="参赛队伍专用"><Link className="button primary" to="/team">查看当前账号</Link></AccessNotice>
    }
    if (!team_id) {
      return (
        <AccessNotice title="加入队伍后可使用策略验证">
          <Link className="button primary" to="/team" state={{ returnTo }}>前往组队</Link>
        </AccessNotice>
      )
    }
    const access: ValidationAccess = { scope: `${id}:${team_id}`, csrf: csrf_token }
    return <Outlet key={access.scope} context={access} />
  }

  if (session.data.kind === 'anonymous') {
    if (demo.isPending) return <Loading>正在读取验证权限…</Loading>
    if (demo.isSuccess) return <Outlet key="demo" context={{ scope: 'demo' } satisfies ValidationAccess} />
    if (!(demo.error instanceof ApiError && demo.error.code === 'LOGIN_REQUIRED')) {
      return <Feedback error={demo.error} retry={() => void demo.refetch()} />
    }
  }

  return (
    <AccessNotice title={session.data.kind === 'expired' ? '会话已失效，请重新登录' : '登录后可使用策略验证'}>
      <Link className="button primary" to="/login" state={{ returnTo }}>前往登录</Link>
    </AccessNotice>
  )
}

function AccessNotice({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="panel identity-panel">
      <div className="panel-heading"><h1 className="access-title">{title}</h1></div>
      {children}
    </section>
  )
}
