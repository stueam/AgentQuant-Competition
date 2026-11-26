const money = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' })
const percent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 2 })
const integer = new Intl.NumberFormat('zh-CN')
const beijingTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  dateStyle: 'medium',
  timeStyle: 'short',
  hourCycle: 'h23',
})

export const formatMoney = (value: string | number) => money.format(Number(value))
export const formatPercent = (value: string | number) => percent.format(Number(value))
export const formatInteger = (value: number) => integer.format(value)
export const formatTime = (value: string) => beijingTime.format(new Date(value))

export function errorMessage(error: Error) {
  return error instanceof TypeError ? '暂时无法连接服务，请检查连接后重试。' : error.message
}
