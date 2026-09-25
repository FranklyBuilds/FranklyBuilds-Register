<script setup lang='ts'>
import { computed } from 'vue'
import type { OutlookRegisterSnapshot } from '@/services/outlookGateway'

const props = defineProps<{ snapshot: OutlookRegisterSnapshot | null; loadError?: string }>()
const stats = computed(() => props.snapshot?.stats)
function numeric(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}
function count(value: unknown) { return numeric(value) ? String(value) : '—' }
function elapsed(value: unknown) {
  if (!numeric(value)) return '—'
  const seconds = Math.floor(value)
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor(seconds % 3600 / 60)
  return `${hours ? `${hours}小时` : ''}${minutes || hours ? `${minutes}分` : ''}${seconds % 60}秒`
}
const phase = computed(() => {
  if (!props.snapshot) return { text: '尚未读取任务状态', type: 'info' as const }
  switch (props.snapshot.status) {
    case 'running': return { text: '执行中', type: 'primary' as const }
    case 'stopping': return { text: '停止中，等待收尾', type: 'warning' as const }
    case 'stopped': return { text: '已停止', type: 'info' as const }
    case 'failed': return { text: '执行失败', type: 'danger' as const }
    case 'completed':
      if ((stats.value?.failed ?? 0) > 0) return { text: '执行结束（含失败）', type: 'warning' as const }
      if (stats.value?.submitted === 0) return { text: '执行结束（没有执行任何任务）', type: 'info' as const }
      return { text: '执行结束，请核对结果', type: 'info' as const }
    case 'idle': return { text: '空闲', type: 'info' as const }
    default: return { text: `未知状态：${props.snapshot.status}`, type: 'warning' as const }
  }
})
const metrics = computed(() => {
  const current = stats.value
  const rate = current?.success_rate
  return [
    { key: 'submitted', label: '已提交', value: count(current?.submitted) },
    { key: 'running', label: '运行中', value: count(current?.running) },
    { key: 'succeeded', label: '成功', value: count(current?.succeeded) },
    { key: 'failed', label: '失败', value: count(current?.failed) },
    { key: 'success-rate', label: '已结束任务成功率', value: numeric(rate) && rate <= 100 ? `${rate.toFixed(1)}%` : '—' },
    { key: 'elapsed', label: '运行耗时', value: elapsed(current?.elapsed_seconds) },
    { key: 'batch', label: '当前批次', value: count(current?.batch_index) },
  ]
})
const failures = computed(() => Object.entries(props.snapshot?.failure_stats ?? {})
  .filter(([, value]) => numeric(value) && value > 0)
  .sort(([a, av], [b, bv]) => bv - av || a.localeCompare(b))
  .map(([reason, count]) => ({ reason, count })))
</script>

<template>
  <section class='task-status' data-testid='outlook-task-status' aria-label='Outlook 独立任务运行状态'>
    <div class='status-heading'>
      <el-tag :type='phase.type'>{{ phase.text }}</el-tag>
      <span class='muted'>代理组：{{ snapshot?.proxyGroup ?? '—' }}{{ snapshot?.proxyGroup === '' ? '默认组' : '' }} · 可用代理：{{ count(snapshot?.proxyCount) }}</span>
    </div>
    <el-alert v-if='loadError' type='warning' :closable='false' show-icon :title='loadError' role='alert' data-testid='outlook-task-stale' />
    <dl class='metrics'>
      <div v-for='metric in metrics' :key='metric.key'>
        <dt>{{ metric.label }}</dt>
        <dd :data-testid='`outlook-metric-${metric.key}`'>{{ metric.value }}</dd>
      </div>
    </dl>
    <p v-if='snapshot?.error' class='task-error' role='alert'>错误代码：{{ snapshot.error }}；具体原因请查看下方脱敏日志。</p>
    <div class='failure-heading'>失败分类 <span class='muted'>按后端诊断事件计数，不等同于失败任务数；流程结束不代表注册成功。</span></div>
    <el-table v-if='failures.length' :data='failures' size='small' max-height='180' aria-label='Outlook 失败分类'>
      <el-table-column prop='reason' label='失败分类 / 原始键' min-width='220' />
      <el-table-column prop='count' label='事件次数' width='100' />
    </el-table>
    <p v-else class='muted'>{{ snapshot?.failure_stats ? '暂无失败分类记录' : '尚未获取失败统计' }}</p>
  </section>
</template>

<style scoped>
.task-status { margin-bottom: 16px; padding: 14px; border: 1px solid var(--el-border-color-light); border-radius: 6px; }
.status-heading { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(115px, 1fr)); gap: 14px; margin: 16px 0; }
dt, .muted { color: var(--el-text-color-secondary); font-size: 12px; }
dd { margin: 6px 0 0; font-size: 20px; font-weight: 600; overflow-wrap: anywhere; }
.task-error { color: var(--el-color-danger); overflow-wrap: anywhere; }
.failure-heading { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 8px; font-size: 13px; }
</style>
