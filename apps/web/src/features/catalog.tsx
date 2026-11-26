import { useQuery } from '@tanstack/react-query'
import { Database, Layers3 } from 'lucide-react'
import { catalogQuery } from '../queries'
import { Feedback, Loading } from '../ui'

export function CatalogPage() {
  const catalog = useQuery(catalogQuery)
  return (
    <>
      <header className="page-heading"><h1>数据目录</h1><p>查看验证数据集和可提交的内置策略。</p></header>
      {catalog.isPending && <Loading>正在读取目录…</Loading>}
      {catalog.isError && <Feedback error={catalog.error} retry={() => void catalog.refetch()} />}
      {catalog.data && (
        <div className="workspace-grid catalog-grid">
          <section className="panel">
            <div className="panel-heading"><span className="section-icon blue"><Database size={20} /></span><h2>验证数据集</h2></div>
            <CatalogList items={catalog.data.datasets} empty="暂无可用验证数据集。" />
          </section>
          <section className="panel">
            <div className="panel-heading"><span className="section-icon"><Layers3 size={20} /></span><h2>内置策略</h2></div>
            <CatalogList items={catalog.data.baselines} empty="暂无可用内置策略。" />
          </section>
        </div>
      )}
    </>
  )
}

function CatalogList({ items, empty }: { items: { id: string; name: string }[]; empty: string }) {
  if (!items.length) return <p className="empty">{empty}</p>
  return (
    <ul className="catalog-list">
      {items.map((item) => <li key={item.id}><strong>{item.name}</strong><code>{item.id}</code></li>)}
    </ul>
  )
}
