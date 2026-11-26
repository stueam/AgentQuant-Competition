import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, LogOut, UserRound, UsersRound } from 'lucide-react'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { api } from '../api'
import type { Identity, Team } from '../api'
import { teamQuery } from '../queries'
import { announceSessionChange, sessionQuery, setSession } from '../session'
import { Feedback, Loading } from '../ui'
import { validationReturnTo } from './account'

const roles: Record<Identity['role'], string> = { participant: '参赛选手', organizer: '组织者', reviewer: '评委' }

export function TeamPage() {
  const session = useQuery(sessionQuery)
  const location = useLocation()
  const returnTo = validationReturnTo(location.state)

  return (
    <>
      <header className="page-heading"><h1>我的队伍</h1></header>
      {session.isPending && <Loading>正在读取会话…</Loading>}
      {session.isError && <Feedback error={session.error} retry={() => void session.refetch()} />}
      {session.isSuccess && (session.data.kind === 'authenticated' ? (
        <AccountTeam identity={session.data.identity} returnTo={returnTo} key={session.data.identity.id} />
      ) : (
        <section className="panel identity-panel">
          <div className="panel-heading">
            <span className="section-icon"><UsersRound size={20} /></span>
            <h2>{session.data.kind === 'expired' ? '会话已失效，请重新登录' : '登录后可创建或加入队伍'}</h2>
          </div>
          <Link className="button primary" to="/login" state={{ returnTo }}>前往登录 <ArrowRight size={16} aria-hidden="true" /></Link>
        </section>
      ))}
    </>
  )
}

function AccountTeam({ identity, returnTo }: { identity: Identity; returnTo?: string }) {
  const [inviteCode, setInviteCode] = useState('')
  const client = useQueryClient()
  const navigate = useNavigate()
  const logout = useMutation({
    mutationFn: () => api.logout(identity.csrf_token),
    onSuccess: () => {
      setSession(client, { kind: 'anonymous' })
      announceSessionChange()
      navigate('/login', { replace: true })
    },
  })

  function showTeam(team: Team, code = '') {
    const current = client.getQueryData(sessionQuery.queryKey)
    if (current?.kind !== 'authenticated' || current.identity.csrf_token !== identity.csrf_token) return
    const member = team.members.find((item) => item.id === identity.id)
    setSession(client, { kind: 'authenticated', identity: { ...identity, team_id: team.id, captain: member?.captain ?? false } })
    client.setQueryData(teamQuery(identity.id, team.id).queryKey, team)
    setInviteCode(code)
    announceSessionChange()
  }

  return (
    <div className="team-layout">
      <section className="panel identity-panel">
        <div className="panel-heading">
          <span className="section-icon"><UserRound size={20} /></span>
          <div><h2>{identity.username}</h2><p>{roles[identity.role]}</p></div>
          <button type="button" className="button secondary logout-button" onClick={() => logout.mutate()} disabled={logout.isPending}>
            <LogOut size={16} aria-hidden="true" />{logout.isPending ? '正在退出…' : '退出登录'}
          </button>
        </div>
        {logout.isError && <Feedback error={logout.error} />}
        {identity.role !== 'participant' && <p className="muted">参赛选手账号可在此创建或加入队伍。</p>}
      </section>
      {identity.role === 'participant' && (identity.team_id ? (
        <TeamDetails identityId={identity.id} teamId={identity.team_id} inviteCode={inviteCode} returnTo={returnTo} />
      ) : <TeamForms csrf={identity.csrf_token} onTeam={showTeam} disabled={logout.isPending} />)}
    </div>
  )
}

function TeamDetails({ identityId, teamId, inviteCode, returnTo }: {
  identityId: string; teamId: string; inviteCode: string; returnTo?: string
}) {
  const team = useQuery(teamQuery(identityId, teamId))
  return (
    <section className="panel identity-panel">
      <div className="panel-heading">
        <span className="section-icon blue"><UsersRound size={20} /></span>
        <h2>{team.data?.name ?? '队伍详情'}</h2>
      </div>
      {team.isPending && <Loading>正在读取队伍成员…</Loading>}
      {team.isError && <Feedback error={team.error} retry={() => void team.refetch()} />}
      {inviteCode && (
        <div className="team-invite">
          <p><strong>组队码</strong></p>
          <code>{inviteCode}</code>
          <p className="muted">组队码只在建队后显示，请保存并分享给队友。</p>
        </div>
      )}
      {team.isSuccess && (
        <ul className="catalog-list">
          {team.data.members.map((member) => (
            <li key={member.id}><strong>{member.username}</strong><span className="muted">{member.captain ? '队长' : '成员'}</span></li>
          ))}
        </ul>
      )}
      <div className="identity-actions">
        <Link className="button primary" to={returnTo ?? '/'}>进入策略验证 <ArrowRight size={16} aria-hidden="true" /></Link>
      </div>
    </section>
  )
}

function TeamForms({ csrf, disabled, onTeam }: { csrf: string; disabled: boolean; onTeam: (team: Team, code?: string) => void }) {
  const create = useMutation({
    mutationFn: (name: string) => api.createTeam({ name }, csrf),
    onSuccess: ({ invite_code, ...team }) => onTeam(team, invite_code),
    gcTime: 0,
  })
  const join = useMutation({
    mutationFn: (invite_code: string) => api.joinTeam({ invite_code }, csrf),
    onSuccess: (team) => onTeam(team),
    gcTime: 0,
  })
  const busy = disabled || create.isPending || join.isPending

  function submit(event: FormEvent<HTMLFormElement>, field: string, mutate: (value: string) => void) {
    event.preventDefault()
    const value = String(new FormData(event.currentTarget).get(field)).trim()
    if (value && !busy) mutate(value)
  }

  return (
    <div className="workspace-grid catalog-grid">
      <section className="panel">
        <div className="panel-heading"><span className="section-icon"><UsersRound size={20} /></span><h2>创建队伍</h2></div>
        <form className="submission-form" onSubmit={(event) => submit(event, 'team_name', create.mutate)}>
          <label htmlFor="team-name">队名</label>
          <input id="team-name" name="team_name" required maxLength={80} disabled={busy} />
          {create.isError && <Feedback error={create.error} />}
          <button type="submit" className="button primary" disabled={busy}>{create.isPending ? '正在创建…' : '创建队伍'}</button>
        </form>
      </section>
      <section className="panel">
        <div className="panel-heading"><span className="section-icon blue"><UsersRound size={20} /></span><h2>加入队伍</h2></div>
        <form className="submission-form" onSubmit={(event) => submit(event, 'team_invite', join.mutate)}>
          <label htmlFor="team-invite-code">组队码</label>
          <input id="team-invite-code" name="team_invite" required minLength={32} maxLength={128} disabled={busy}
            autoComplete="off" spellCheck={false} />
          {join.isError && <Feedback error={join.error} />}
          <button type="submit" className="button primary" disabled={busy}>{join.isPending ? '正在加入…' : '加入队伍'}</button>
        </form>
      </section>
    </div>
  )
}
