import { queryOptions } from '@tanstack/react-query'
import { api } from './api'
import type { Run } from './api'

export const catalogQuery = queryOptions({
  queryKey: ['catalog'],
  queryFn: ({ signal }) => api.catalog(signal),
  staleTime: 60_000,
})

export function submissionsQuery(scope: string) {
  return queryOptions({
    queryKey: ['participant', scope, 'submissions'],
    queryFn: ({ signal }) => api.submissions(signal, scope !== 'demo'),
  })
}

export function runQuery(scope: string, id: string) {
  return queryOptions({
    queryKey: ['participant', scope, 'runs', id],
    queryFn: ({ signal }) => api.run(id, signal, scope !== 'demo'),
  })
}

export function resultQuery(scope: string, id: string) {
  return queryOptions({
    queryKey: ['participant', scope, 'runs', id, 'result'],
    queryFn: ({ signal }) => api.result(id, signal, scope !== 'demo'),
    staleTime: Infinity,
  })
}

export function teamQuery(identityId: string, teamId: string) {
  return queryOptions({
    queryKey: ['participant', identityId, 'team', teamId],
    queryFn: ({ signal }) => api.team(teamId, signal),
  })
}

export function isPending(run?: Run) {
  return run?.status === 'QUEUED' || run?.status === 'RUNNING'
}
