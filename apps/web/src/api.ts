import type { components } from './api.generated'

type Schema = components['schemas']
export type Catalog = Schema['Catalog']
export type Submission = Schema['Submission']
export type Run = Schema['Run']
export type Result = Schema['EvaluationResult']
export type Login = Schema['Login']
export type Register = Schema['Register']
export type Identity = Schema['Identity']
export type Team = Schema['Team']

export class ApiError extends Error {
  constructor(readonly status: number, message: string, readonly code?: string) {
    super(message)
  }
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(`/api/v1${path}`, { cache: 'no-store', ...init })
  if (!response.ok) {
    const error = await response.json().catch(() => null)
    const message = typeof error?.message === 'string' ? error.message : (
      response.status === 422 ? '请检查输入字段' : `请求失败（${response.status}），请重试`
    )
    throw new ApiError(response.status, message, typeof error?.code === 'string' ? error.code : undefined)
  }
  return response
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init)
  return response.json() as Promise<T>
}

function post(body?: object, csrf?: string, extraHeaders?: Record<string, string>): RequestInit {
  const headers = new Headers(extraHeaders)
  if (csrf) headers.set('X-CSRF-Token', csrf)
  if (body) headers.set('Content-Type', 'application/json')
  return { method: 'POST', headers, body: body && JSON.stringify(body) }
}

function validationCredentials(authenticated: boolean): RequestCredentials {
  return authenticated ? 'same-origin' : 'omit'
}

export const api = {
  me: (signal: AbortSignal) => json<Identity>('/me', { signal }),
  login: (body: Login) => json<Identity>('/auth/login', post(body)),
  register: async (body: Register) => { await request('/auth/register', post(body)) },
  logout: async (csrf: string) => { await request('/auth/logout', post(undefined, csrf)) },
  team: (id: string, signal: AbortSignal) => json<Team>(`/teams/${encodeURIComponent(id)}`, { signal }),
  createTeam: (body: Schema['TeamCreate'], csrf: string) => json<Schema['CreatedTeam']>('/teams', post(body, csrf)),
  joinTeam: (body: Schema['JoinTeam'], csrf: string) => json<Team>('/teams/join', post(body, csrf)),
  catalog: (signal: AbortSignal) => json<Catalog>('/catalog', { signal }),
  submissions: (signal: AbortSignal, authenticated: boolean) =>
    json<Submission[]>('/submissions', { signal, credentials: validationCredentials(authenticated) }),
  submit: (body: Schema['CreateSubmission'], csrf?: string) =>
    json<Submission>('/submissions', { ...post(body, csrf), credentials: validationCredentials(Boolean(csrf)) }),
  start: (body: Schema['CreateRun'], key: string, csrf?: string) =>
    json<Run>('/runs', { ...post(body, csrf, { 'Idempotency-Key': key }), credentials: validationCredentials(Boolean(csrf)) }),
  run: (id: string, signal: AbortSignal, authenticated: boolean) =>
    json<Run>(`/runs/${encodeURIComponent(id)}`, { signal, credentials: validationCredentials(authenticated) }),
  result: (id: string, signal: AbortSignal, authenticated: boolean) =>
    json<Result>(`/runs/${encodeURIComponent(id)}/result`, { signal, credentials: validationCredentials(authenticated) }),
}
