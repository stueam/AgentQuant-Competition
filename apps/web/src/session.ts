import { queryOptions } from '@tanstack/react-query'
import type { QueryClient } from '@tanstack/react-query'
import { api, ApiError } from './api'
import type { Identity } from './api'

type Session = { kind: 'authenticated'; identity: Identity } | { kind: 'anonymous' } | { kind: 'expired' }

export type ValidationAccess = { scope: string; csrf?: string }

export const sessionQuery = queryOptions({
  queryKey: ['session'],
  queryFn: async ({ signal }): Promise<Session> => {
    try {
      return { kind: 'authenticated', identity: await api.me(signal) }
    } catch (error) {
      if (error instanceof ApiError) {
        if (error.code === 'LOGIN_REQUIRED') return { kind: 'anonymous' }
        if (error.code === 'SESSION_EXPIRED') return { kind: 'expired' }
      }
      throw error
    }
  },
})

const privateQueries = { queryKey: ['participant'] }

export function setSession(client: QueryClient, session: Session) {
  void client.cancelQueries({ queryKey: sessionQuery.queryKey })
  client.removeQueries(privateQueries)
  client.setQueryData(sessionQuery.queryKey, session)
}

export const sessionChangeKey = 'agentquant.session-change'

export function announceSessionChange() {
  try {
    localStorage.setItem(sessionChangeKey, String(Date.now()))
  } catch {
    // The session query still checks the cookie when a tab regains focus.
  }
}

export function recheckSession(client: QueryClient) {
  client.removeQueries(privateQueries)
  void client.resetQueries({ queryKey: sessionQuery.queryKey })
}

export function handleSessionError(client: QueryClient, error: Error) {
  if (!(error instanceof ApiError)) return

  const session = client.getQueryData<Session>(sessionQuery.queryKey)
  if (error.code === 'SESSION_EXPIRED' || (error.code === 'LOGIN_REQUIRED' && session?.kind === 'authenticated')) {
    setSession(client, { kind: 'expired' })
    return
  }
  if (error.code === 'CSRF_INVALID') recheckSession(client)
}
