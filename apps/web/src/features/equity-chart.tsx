import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { init, use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { useEffect, useRef } from 'react'
import type { Result } from '../api'
import { formatMoney } from '../format'

use([LineChart, GridComponent, TooltipComponent, CanvasRenderer])

const axisNumber = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 })

export default function EquityChart({ points }: { points: Result['equity'] }) {
  const container = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const element = container.current
    if (!element) return

    const chart = init(element)
    chart.setOption({
      animation: false,
      grid: { top: 19, right: 24, bottom: 12, left: 4, outerBoundsMode: 'same' },
      tooltip: {
        trigger: 'axis',
        renderMode: 'richText',
        valueFormatter: (value: number) => formatMoney(value),
      },
      xAxis: {
        type: 'category',
        boundaryGap: true,
        data: points.map((point) => point.trading_date),
        axisTick: { show: false },
        axisLabel: { color: '#51637b', hideOverlap: true },
        axisLine: { lineStyle: { color: '#dbe4ee' } },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { color: '#51637b', formatter: (value: number) => axisNumber.format(value) },
        splitLine: { lineStyle: { color: '#e7edf4', type: 'dashed' } },
      },
      series: [{
        type: 'line',
        name: '净值',
        data: points.map((point) => Number(point.equity)),
        lineStyle: { color: '#007d72', width: 3 },
        itemStyle: { color: '#007d72' },
        symbolSize: 8,
        showSymbol: points.length <= 30,
        areaStyle: { color: 'rgba(0, 125, 114, 0.09)' },
      }],
    })
    const resize = new ResizeObserver(() => chart.resize())
    resize.observe(element)
    return () => {
      resize.disconnect()
      chart.dispose()
    }
  }, [points])

  return <div ref={container} className="equity-chart" aria-hidden="true" />
}
