import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import type { FormEvent } from 'react'
import { api } from './api'
import type { Catalog, Result, Run, Submission } from './api'
import './styles.css'

const money = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' })
const percent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 2 })
const statuses = { QUEUED: '排队中', RUNNING: '运行中', SUCCEEDED: '已完成', FAILED: '失败' }

function App() {
  const [catalog, setCatalog] = useState<Catalog>()
  const [submissions, setSubmissions] = useState<Submission[]>([])
  const [baseline, setBaseline] = useState('')
  const [name, setName] = useState('')
  const [run, setRun] = useState<Run>()
  const [result, setResult] = useState<Result>()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [loadAttempt, setLoadAttempt] = useState(0)
  const [pollAttempt, setPollAttempt] = useState(0)
  const [requestKey, setRequestKey] = useState<{ submissionId: string; key: string }>()
  const activeRunId = run?.id
  const pending = run?.status === 'RUNNING' || run?.status === 'QUEUED'

  useEffect(() => {
    let current = true
    Promise.all([api.catalog(), api.submissions()]).then(([items, saved]) => {
      if (!current) return
      setCatalog(items)
      setSubmissions(saved)
      setBaseline(items.baselines.find((item) => item.id === 'factor-rank')?.id ?? items.baselines[0]?.id ?? '')
    }).catch((reason: Error) => { if (current) setError(reason.message) })
    return () => { current = false }
  }, [loadAttempt])

  useEffect(() => {
    if (!activeRunId || !pending) return
    let current = true
    let timer: ReturnType<typeof setTimeout>
    async function poll(id: string) {
      try {
        const latest = await api.run(id)
        if (current) {
          setRun(latest)
          if (latest.status === 'QUEUED' || latest.status === 'RUNNING') {
            timer = setTimeout(() => void poll(id), 750)
          }
        }
      } catch (reason) {
        if (current) setError((reason as Error).message)
      }
    }
    timer = setTimeout(() => void poll(activeRunId), 500)
    return () => { current = false; clearTimeout(timer) }
  }, [activeRunId, pending, pollAttempt])

  useEffect(() => {
    if (run?.status !== 'SUCCEEDED') return
    let current = true
    api.result(run.id).then((value) => {
      if (current) setResult(value)
    }).catch((reason: Error) => { if (current) setError(reason.message) })
    return () => { current = false }
  }, [run?.id, run?.status, pollAttempt])

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    setBusy(true)
    try {
      const item = await api.submit({ name, source: { kind: 'baseline', baseline_id: baseline } })
      setSubmissions((items) => [item, ...items])
      setName('')
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function start(item: Submission) {
    const datasetId = catalog?.datasets[0]?.id
    if (!datasetId) return
    const key = requestKey?.submissionId === item.id ? requestKey.key : crypto.randomUUID()
    setRequestKey({ submissionId: item.id, key })
    setBusy(true)
    setError('')
    try {
      const queued = await api.start({ submission_id: item.id, dataset_id: datasetId }, key)
      setResult(undefined)
      setRun(queued)
      setRequestKey(undefined)
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setBusy(false)
    }
  }

  function retryLoad() {
    setError('')
    if (!catalog) setLoadAttempt((value) => value + 1)
    else setPollAttempt((value) => value + 1)
  }

  const baselineNames = new Map(catalog?.baselines.map((entry) => [entry.id, entry.name]))

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner"><strong>AgentQuant</strong><span>第 31 届智能体大赛 · 赛道 2</span></div>
      </header>
      <main>
        <div className="intro">
          <h1>策略验证</h1>
          <p>使用内置策略和人工行情，验证日频评测流程。</p>
        </div>
        {error && (
          <div role="alert" className="notice">
            {error}
            {(!catalog || pending || run?.status === 'SUCCEEDED') && <button onClick={retryLoad}>重试读取</button>}
          </div>
        )}
        <div className="workspace">
          <section className="panel">
            <h2>新建提交</h2>
            <form onSubmit={save}>
              <label>提交名称<input required maxLength={80} value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：因子排序基线" /></label>
              <label>内置策略
                <select value={baseline} onChange={(event) => setBaseline(event.target.value)} disabled={!catalog}>
                  {catalog?.baselines.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}
                </select>
              </label>
              <button className="primary" disabled={!baseline || !name.trim() || busy}>保存提交</button>
            </form>
          </section>
          <section className="panel">
            <h2>提交记录</h2>
            <p className="muted">{catalog?.datasets[0]?.name ?? '正在读取验证数据'}</p>
            {submissions.length === 0 && <p className="empty">保存提交后即可发起验证。</p>}
            <ul className="submissions">
              {submissions.map((item) => (
                <li key={item.id}>
                  <div><strong>{item.name}</strong><small>{item.source.kind === 'baseline' ? baselineNames.get(item.source.baseline_id) : '策略 ZIP'}</small></div>
                  <button disabled={busy || pending} onClick={() => void start(item)}>发起验证</button>
                </li>
              ))}
            </ul>
          </section>
        </div>
        {run && (
          <section className="panel result">
            <div className="result-heading"><h2>验证结果</h2><span role="status" className="status">{statuses[run.status]}</span></div>
            <p className="muted">{submissions.find((item) => item.id === run.submission_id)?.name} · {run.rules_version}</p>
            {run.error && <p role="alert">{run.error.message}</p>}
            {result && <ResultView result={result} />}
          </section>
        )}
      </main>
    </>
  )
}

function ResultView({ result }: { result: Result }) {
  const metrics = result.metrics
  return (
    <>
      <dl className="metrics">
        <div><dt>期末净值</dt><dd>{money.format(Number(metrics.final_equity))}</dd></div>
        <div><dt>净收益率</dt><dd>{percent.format(Number(metrics.net_return))}</dd></div>
        <div><dt>最大回撤</dt><dd>{percent.format(Number(metrics.max_drawdown))}</dd></div>
        <div><dt>交易费用</dt><dd>{money.format(Number(metrics.total_fees))}</dd></div>
      </dl>
      <div className="tables">
        <div className="table-scroll">
          <table>
            <caption>逐日净值</caption>
            <thead><tr><th>交易日</th><th>净值</th></tr></thead>
            <tbody>{result.equity.map((point) => <tr key={point.trading_date}><td>{point.trading_date}</td><td>{money.format(Number(point.equity))}</td></tr>)}</tbody>
          </table>
        </div>
        <div className="table-scroll">
          <table>
            <caption>成交明细</caption>
            <thead><tr><th>交易日</th><th>标的</th><th>方向</th><th>数量</th><th>价格</th></tr></thead>
            <tbody>
              {result.trades.length === 0 && <tr><td colSpan={5}>本次验证无成交</td></tr>}
              {result.trades.map((trade) => (
                <tr key={`${trade.trading_date}-${trade.asset_id}-${trade.side}`}>
                  <td>{trade.trading_date}</td><td>{trade.asset_id}</td><td>{trade.side === 'BUY' ? '买入' : '卖出'}</td>
                  <td>{trade.shares.toLocaleString('zh-CN')}</td><td>{money.format(Number(trade.price))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}

createRoot(document.getElementById('root')!).render(<App />)
