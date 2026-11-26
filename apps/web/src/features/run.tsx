import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, ChartNoAxesCombined, CircleAlert, Clock3, ReceiptText } from 'lucide-react'
import { lazy, Suspense, useState } from 'react'
import { Link, useOutletContext, useParams } from 'react-router-dom'
import type { Result, Run } from '../api'
import { formatInteger, formatMoney, formatPercent, formatTime } from '../format'
import { catalogQuery, isPending, resultQuery, runQuery, submissionsQuery } from '../queries'
import type { ValidationAccess } from '../session'
import { Feedback, Loading } from '../ui'

const EquityChart = lazy(() => import('./equity-chart'))
const statuses: Record<Run['status'], string> = {
  QUEUED: '排队中',
  RUNNING: '运行中',
  SUCCEEDED: '已完成',
  FAILED: '评测失败',
}
const pageSize = 10

export default function RunPage() {
  const { scope } = useOutletContext<ValidationAccess>()
  const { runId = '' } = useParams()
  const run = useQuery({
    ...runQuery(scope, runId),
    refetchInterval: ({ state }) => !state.error && isPending(state.data) ? 2_000 : false,
  })
  const catalog = useQuery(catalogQuery)
  const submissions = useQuery(submissionsQuery(scope))
  const result = useQuery({
    ...resultQuery(scope, runId),
    enabled: run.data?.status === 'SUCCEEDED',
  })
  const submissionName = submissions.data?.find((item) => item.id === run.data?.submission_id)?.name
  const datasetName = catalog.data?.datasets.find((item) => item.id === run.data?.dataset_id)?.name

  return (
    <>
      <Link to="/" className="text-link back-link"><ArrowLeft size={16} aria-hidden="true" />返回策略验证</Link>
      <header className="page-heading">
        <h1>验证结果</h1>
        {submissionName && <p>{submissionName}</p>}
      </header>
      {run.isPending && <Loading>正在读取验证任务…</Loading>}
      {run.isError && <Feedback error={run.error} retry={() => void run.refetch()} />}
      {run.data && (
        <section className="panel run-panel" aria-labelledby="run-title">
          <div className="panel-heading">
            <span className="section-icon"><Clock3 size={20} /></span>
            <h2 id="run-title">任务状态</h2>
            <span className={`badge status-${run.data.status.toLowerCase()}`} role="status">{statuses[run.data.status]}</span>
          </div>
          <dl className="metadata">
            <div><dt>验证数据</dt><dd>{datasetName ?? run.data.dataset_id}</dd></div>
            <div><dt>规则版本</dt><dd><code>{run.data.rules_version}</code></dd></div>
            <div><dt>创建时间（北京时间）</dt><dd><time dateTime={run.data.created_at}>{formatTime(run.data.created_at)}</time></dd></div>
            <div><dt>任务 ID</dt><dd><code>{runId}</code></dd></div>
          </dl>
          {!run.isError && run.data.status === 'QUEUED' && <Loading>正在等待评测进程领取任务…</Loading>}
          {!run.isError && run.data.status === 'RUNNING' && <Loading>正在运行日频评测…</Loading>}
          {run.data.status === 'FAILED' && (
            <div className="feedback" role="alert">
              <CircleAlert size={17} aria-hidden="true" />
              <span>{run.data.error?.message ?? '评测未能完成。'} {run.data.error && <code>{run.data.error.code}</code>}</span>
            </div>
          )}
        </section>
      )}
      {run.data?.status === 'SUCCEEDED' && result.isPending && <Loading>正在读取评测结果…</Loading>}
      {result.isError && <Feedback error={result.error} retry={() => void result.refetch()} />}
      {result.data && <ResultDetails result={result.data} key={runId} />}
    </>
  )
}

function ResultDetails({ result }: { result: Result }) {
  const { metrics, equity, trades } = result
  const [tradePage, setTradePage] = useState(0)
  const pageCount = Math.ceil(trades.length / pageSize)
  const pageTrades = trades.slice(tradePage * pageSize, (tradePage + 1) * pageSize)

  return (
    <>
      <dl className="metrics" aria-label="关键指标">
        <div><dt>期末净值</dt><dd>{formatMoney(metrics.final_equity)}</dd></div>
        <div><dt>净收益率</dt><dd>{formatPercent(metrics.net_return)}</dd></div>
        <div><dt>最大回撤</dt><dd>{formatPercent(metrics.max_drawdown)}</dd></div>
        <div><dt>交易费用</dt><dd>{formatMoney(metrics.total_fees)}</dd></div>
      </dl>
      <section className="panel chart-panel" aria-labelledby="equity-title">
        <div className="panel-heading">
          <span className="section-icon"><ChartNoAxesCombined size={20} /></span>
          <div><h2 id="equity-title">净值曲线</h2><p>初始资金 {formatMoney(metrics.initial_cash)}</p></div>
        </div>
        {equity.length ? (
          <Suspense fallback={<Loading>正在加载净值曲线…</Loading>}><EquityChart points={equity} /></Suspense>
        ) : <p className="empty">本次验证暂无净值记录。</p>}
        {!!equity.length && (
          <details>
            <summary>查看逐日净值</summary>
            <div className="table-scroll" role="region" aria-label="逐日净值" tabIndex={0}>
              <table>
                <thead><tr><th scope="col">交易日</th><th scope="col" className="numeric">净值</th></tr></thead>
                <tbody>{equity.map((point) => (
                  <tr key={point.trading_date}>
                    <td><time dateTime={point.trading_date}>{point.trading_date}</time></td>
                    <td className="numeric">{formatMoney(point.equity)}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </details>
        )}
      </section>
      <section className="panel trade-panel" aria-labelledby="trades-title">
        <div className="panel-heading">
          <span className="section-icon blue"><ReceiptText size={20} /></span>
          <div><h2 id="trades-title">成交明细</h2><p>共 {formatInteger(trades.length)} 笔成交</p></div>
        </div>
        {!trades.length && <p className="empty">本次验证无成交。</p>}
        {!!trades.length && (
          <div className="table-scroll" tabIndex={0} role="region" aria-label="成交明细">
            <table>
              <thead>
                <tr><th scope="col">交易日</th><th scope="col">标的</th><th scope="col">方向</th>
                  <th scope="col" className="numeric">数量</th><th scope="col" className="numeric">价格</th>
                  <th scope="col" className="numeric">费用</th></tr>
              </thead>
              <tbody>{pageTrades.map((trade, index) => (
                <tr key={tradePage * pageSize + index}>
                  <td><time dateTime={trade.trading_date}>{trade.trading_date}</time></td>
                  <td><code>{trade.asset_id}</code></td>
                  <td>{trade.side === 'BUY' ? '买入' : '卖出'}</td>
                  <td className="numeric">{formatInteger(trade.shares)}</td>
                  <td className="numeric">{formatMoney(trade.price)}</td>
                  <td className="numeric">{formatMoney(trade.fee)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
        {pageCount > 1 && (
          <nav className="pagination" aria-label="成交分页">
            <button className="button secondary" onClick={() => setTradePage(tradePage - 1)} disabled={tradePage === 0}>上一页</button>
            <span aria-live="polite">第 {tradePage + 1} / {pageCount} 页</span>
            <button className="button secondary" onClick={() => setTradePage(tradePage + 1)} disabled={tradePage + 1 === pageCount}>下一页</button>
          </nav>
        )}
      </section>
    </>
  )
}
