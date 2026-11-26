import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, Database, FilePlus2, History, Layers3, Play, TrendingUp } from 'lucide-react'
import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useNavigate, useOutletContext } from 'react-router-dom'
import { api, ApiError } from '../api'
import type { Catalog, Submission } from '../api'
import { formatTime } from '../format'
import { catalogQuery, runQuery, submissionsQuery } from '../queries'
import type { ValidationAccess } from '../session'
import { readLocal, validationStorageKey, writeLocal } from '../storage'
import { Feedback, Loading } from '../ui'

export function Workspace() {
  const { scope, csrf } = useOutletContext<ValidationAccess>()
  const catalog = useQuery(catalogQuery)
  const submissionsOptions = submissionsQuery(scope)
  const submissions = useQuery(submissionsOptions)
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [baselineChoice, setBaselineChoice] = useState('')
  const [datasetChoice, setDatasetChoice] = useState('')
  const baseline = selectedId(catalog.data?.baselines, baselineChoice, 'factor-rank')
  const dataset = selectedId(catalog.data?.datasets, datasetChoice)
  const requestKeys = useRef(new Map<string, string>())
  const save = useMutation({
    mutationFn: () => api.submit({ name: name.trim(), source: { kind: 'baseline', baseline_id: baseline } }, csrf),
    onSuccess: async () => {
      setName('')
      await queryClient.invalidateQueries({ queryKey: submissionsOptions.queryKey })
    },
  })
  const start = useMutation({
    mutationFn: async (submission: Submission) => {
      const storageKey = validationStorageKey(scope, `request.${submission.id}.${dataset}`)
      const key = requestKeys.current.get(storageKey) ?? readLocal(storageKey) ?? newRequestKey()
      requestKeys.current.set(storageKey, key)
      writeLocal(storageKey, key)
      try {
        const run = await api.start({ submission_id: submission.id, dataset_id: dataset }, key, csrf)
        requestKeys.current.delete(storageKey)
        writeLocal(storageKey, null)
        return run
      } catch (error) {
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
          requestKeys.current.delete(storageKey)
          writeLocal(storageKey, null)
        }
        throw error
      }
    },
    onSuccess: (run) => {
      writeLocal(validationStorageKey(scope, `recent.${run.submission_id}`), run.id)
    },
  })

  function startValidation(item: Submission) {
    start.mutate(item, {
      onSuccess: (run) => {
        queryClient.setQueryData(runQuery(scope, run.id).queryKey, run)
        navigate(`/runs/${run.id}`)
      },
    })
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (name.trim() && baseline && !save.isPending) save.mutate()
  }

  const baselineNames = new Map(catalog.data?.baselines.map((item) => [item.id, item.name]))

  return (
    <>
      <section className="workspace-hero" aria-labelledby="workspace-title">
        <div>
          <p className="hero-context"><span /> 第 31 届智能体大赛 · 赛道 2</p>
          <h1 id="workspace-title">策略验证</h1>
          <p>使用内置策略和人工行情，运行日频评测并查看净值与成交。</p>
        </div>
        <div className="hero-mark" aria-hidden="true"><TrendingUp size={86} strokeWidth={1.3} /></div>
      </section>

      <div className="workspace-grid">
        <section className="panel" aria-labelledby="submission-title">
          <div className="panel-heading">
            <span className="section-icon"><FilePlus2 size={20} /></span>
            <div><h2 id="submission-title">新建提交</h2><p>为内置策略保存一个提交版本。</p></div>
          </div>
          {catalog.isPending && <Loading>正在读取策略目录…</Loading>}
          {catalog.isError && <Feedback error={catalog.error} retry={() => void catalog.refetch()} />}
          <form className="submission-form" onSubmit={submit}>
            <label htmlFor="submission-name">提交名称</label>
            <input id="submission-name" required maxLength={80} placeholder="例如：因子排序基线" value={name}
              onChange={(event) => setName(event.target.value)} disabled={save.isPending} />
            <label htmlFor="baseline">内置策略</label>
            <select id="baseline" value={baseline} onChange={(event) => setBaselineChoice(event.target.value)}
              disabled={!baseline || save.isPending}>
              {!baseline && <option value="">暂无可选策略</option>}
              {catalog.data?.baselines.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
            {save.isError && <Feedback error={save.error} />}
            {save.isSuccess && <p className="success-message" role="status">提交已保存</p>}
            <button className="button primary" disabled={!baseline || !name.trim() || save.isPending}>
              <FilePlus2 size={16} aria-hidden="true" />{save.isPending ? '正在保存…' : '保存提交'}
            </button>
          </form>
        </section>

        <section className="panel dataset-panel" aria-labelledby="dataset-title">
          <div className="panel-heading">
            <span className="section-icon blue"><Database size={20} /></span>
            <div><h2 id="dataset-title">验证数据</h2><p>选择发起验证时使用的数据版本。</p></div>
          </div>
          {catalog.isError && <p className="muted">暂时无法读取数据目录。</p>}
          {catalog.isPending && <Loading>正在读取数据目录…</Loading>}
          {catalog.data && (
            <div className="dataset-select">
              <label htmlFor="dataset">数据集</label>
              <select id="dataset" value={dataset} onChange={(event) => setDatasetChoice(event.target.value)}
                disabled={!dataset || start.isPending}>
                {!dataset && <option value="">暂无可用数据集</option>}
                {catalog.data.datasets.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
              </select>
              {dataset && <p className="version"><Layers3 size={15} aria-hidden="true" /><code>{dataset}</code></p>}
            </div>
          )}
          <Link to="/data" className="text-link">查看目录 <ArrowRight size={15} aria-hidden="true" /></Link>
        </section>
      </div>

      <section className="panel history-panel" aria-labelledby="history-title">
        <div className="panel-heading">
          <span className="section-icon"><History size={20} /></span>
          <div><h2 id="history-title">提交历史</h2><p>从已保存的提交发起验证，或打开最近任务。</p></div>
        </div>
        {start.isError && <Feedback error={start.error} />}
        {submissions.isPending && <Loading>正在读取提交记录…</Loading>}
        {submissions.isError && <Feedback error={submissions.error} retry={() => void submissions.refetch()} />}
        {submissions.data?.length === 0 && <p className="empty">暂无提交。保存一个内置策略后即可发起验证。</p>}
        {!!submissions.data?.length && (
          <div className="table-scroll" tabIndex={0} role="region" aria-label="提交历史">
            <table>
              <thead><tr><th scope="col">提交名称</th><th scope="col">策略</th><th scope="col">提交时间（北京时间）</th><th scope="col">操作</th></tr></thead>
              <tbody>
                {submissions.data.map((item) => {
                  const source = item.source
                  const recent = readLocal(validationStorageKey(scope, `recent.${item.id}`))
                  const starting = start.isPending && start.variables.id === item.id
                  return (
                    <tr key={item.id}>
                      <th scope="row" className="submission-name">{item.name}</th>
                      <td>{source.kind === 'baseline' ? baselineNames.get(source.baseline_id) ?? source.baseline_id : '策略 ZIP'}</td>
                      <td><time dateTime={item.created_at}>{formatTime(item.created_at)}</time></td>
                      <td>
                        <div className="row-actions">
                          {recent && <Link className="text-link" to={`/runs/${recent}`}>最近任务</Link>}
                          {source.kind === 'archive' ? <span className="muted">待沙箱试运行</span> : (
                            <button type="button" className="button secondary" disabled={!dataset || start.isPending}
                              onClick={() => startValidation(item)}>
                              <Play size={14} aria-hidden="true" />{starting ? '正在发起…' : '发起验证'}
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  )
}

function selectedId(items: Catalog['baselines'] | undefined, choice: string, preferred?: string) {
  return items?.find((item) => item.id === choice)?.id
    ?? items?.find((item) => item.id === preferred)?.id
    ?? items?.[0]?.id
    ?? ''
}

function newRequestKey() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, '0')).join('')
}
