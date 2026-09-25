import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, describe, expect, it } from 'vitest'
import type { OutlookRegisterSnapshot, OutlookRegisterStats } from '@/services/outlookGateway'
import OutlookTaskStatus from './OutlookTaskStatus.vue'

let wrapper: VueWrapper | undefined
function snapshot(stats: OutlookRegisterStats): OutlookRegisterSnapshot {
  return {
    taskId: 'outlook-register', enabled: false, status: 'completed', stats, failure_stats: {},
    config: { execution_mode: 'authorized', tasks: 1, concurrent_flows: 1, headless: false,
      proxy: { source: 'mongo', mode: 'mongo', group: '' },
      oauth2: { enable_oauth2: true, redirect_url: '', scopes: [] } },
  }
}
function open(value: OutlookRegisterSnapshot | null) {
  wrapper = mount(OutlookTaskStatus, { props: { snapshot: value }, global: { plugins: [ElementPlus] } })
  return wrapper
}
afterEach(() => { wrapper?.unmount(); wrapper = undefined })

describe('Outlook task monitor', () => {
  it.each([
    [{ submitted: 2, succeeded: 0, failed: 2 }, '执行结束（含失败）'],
    [{ submitted: 0, succeeded: 0, failed: 0 }, '执行结束（没有执行任何任务）'],
    [{ submitted: 2, succeeded: 2, failed: 0 }, '执行结束，请核对结果'],
  ] satisfies Array<[OutlookRegisterStats, string]>)('does not equate completed with registration success: %j', (stats, text) => {
    expect(open(snapshot(stats)).text()).toContain(text)
    expect(wrapper!.find('.el-tag--success').exists()).toBe(false)
  })

  it('does not fabricate zero counters before a snapshot is loaded', () => {
    open(null)
    expect(wrapper!.text()).toContain('尚未读取任务状态')
    expect(wrapper!.get('[data-testid=outlook-metric-submitted]').text()).toBe('—')
    expect(wrapper!.text()).toContain('尚未获取失败统计')
  })

  it('shows diagnostic events without summing them into failed tasks', async () => {
    const value = snapshot({ failed: 1, elapsed_seconds: 3661, success_rate: 0 })
    value.failure_stats = { browser_launch_fail: 2, executor_error: 1, unused: 0 }
    open(value)
    await flushPromises()
    expect(wrapper!.text()).toContain('browser_launch_fail')
    expect(wrapper!.text()).toContain('executor_error')
    expect(wrapper!.text()).not.toContain('unused')
    expect(wrapper!.get('[data-testid=outlook-metric-failed]').text()).toBe('1')
    expect(wrapper!.get('[data-testid=outlook-metric-elapsed]').text()).toBe('1小时1分1秒')
    expect(wrapper!.get('[data-testid=outlook-metric-success-rate]').text()).toBe('0.0%')
  })

  it('rejects invalid counters and escapes server-provided descriptions', async () => {
    const value = snapshot({ running: -1, elapsed_seconds: Number.NaN, success_rate: 101 })
    value.error = '<script>BAD_FIXTURE</script>'
    value.failure_stats = { '<img src=x>': 1, negative: -2, infinite: Infinity }
    open(value)
    await flushPromises()
    expect(wrapper!.get('[data-testid=outlook-metric-running]').text()).toBe('—')
    expect(wrapper!.get('[data-testid=outlook-metric-elapsed]').text()).toBe('—')
    expect(wrapper!.get('[data-testid=outlook-metric-success-rate]').text()).toBe('—')
    expect(wrapper!.find('script').exists()).toBe(false)
    expect(wrapper!.find('img').exists()).toBe(false)
    expect(wrapper!.text()).toContain('<script>BAD_FIXTURE</script>')
    expect(wrapper!.text()).not.toContain('negative')
    expect(wrapper!.text()).not.toContain('infinite')
  })
})
