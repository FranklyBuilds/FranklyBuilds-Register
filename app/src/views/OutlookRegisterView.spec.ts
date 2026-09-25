import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { outlookGateway, type OutlookRegisterConfig, type OutlookRegisterSnapshot } from '@/services/outlookGateway'
import OutlookRegisterView from './OutlookRegisterView.vue'

vi.mock('@/services/outlookGateway', () => ({ outlookGateway: {
  list: vi.fn(), migration: vi.fn(), results: vi.fn(), proxies: vi.fn(), proxyGroups: vi.fn(),
  registerStatus: vi.fn(), registerLogs: vi.fn(), poolStats: vi.fn(), poolAccounts: vi.fn(),
  oauthCheckConfig: vi.fn(), updateRegisterConfig: vi.fn(), startRegister: vi.fn(), stopRegister: vi.fn(), resetRegister: vi.fn(),
} }))
let config: OutlookRegisterConfig
let status: OutlookRegisterSnapshot
let poll: () => unknown
let wrapper: VueWrapper | undefined
function input(label: string) {
  return wrapper!.findAll('input,textarea').find(node => node.attributes('aria-label') === label)!
}
function button(label: string) { return wrapper!.findAll('button').find(node => node.text() === label)! }
async function open() { wrapper = mount(OutlookRegisterView, { global: { plugins: [ElementPlus] } }); await flushPromises() }

beforeEach(() => {
  vi.resetAllMocks()
  vi.spyOn(window, 'setInterval').mockImplementation(callback => { poll = callback as () => unknown; return 123 })
  vi.spyOn(window, 'clearInterval').mockImplementation(() => {})
  config = {
    execution_mode: 'authorized', tasks: 1, concurrent_flows: 1, headless: false,
    proxy: { source: 'mongo', mode: 'mongo', group: '' },
    oauth2: { enable_oauth2: true, redirect_url: 'https://localhost', scopes: ['old.scope'], clientIdConfigured: true },
  }
  status = { taskId: 'outlook-register', enabled: false, status: 'idle', config, stats: {}, log_count: 0 }
  vi.mocked(outlookGateway.list).mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 200 })
  vi.mocked(outlookGateway.migration).mockResolvedValue({ summary: null })
  vi.mocked(outlookGateway.results).mockResolvedValue([])
  vi.mocked(outlookGateway.proxies).mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 100 })
  vi.mocked(outlookGateway.proxyGroups).mockResolvedValue([])
  vi.mocked(outlookGateway.registerStatus).mockImplementation(async () => structuredClone(status))
  vi.mocked(outlookGateway.registerLogs).mockResolvedValue({ items: [] })
  vi.mocked(outlookGateway.poolStats).mockResolvedValue({ stats: { total: 0, registered: 0, oauth2: 0, recovery: 0, sub: 0, oauth_ok: 0, oauth_bad: 0, oauth_unknown: 0 } })
  vi.mocked(outlookGateway.poolAccounts).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 })
  vi.mocked(outlookGateway.oauthCheckConfig).mockResolvedValue({ config: { enabled: false, interval_sec: 3600, delay_ms: 0 } })
})
afterEach(() => { wrapper?.unmount(); wrapper = undefined; vi.restoreAllMocks() })
it('saves OAuth settings from the main page', async () => {
  vi.mocked(outlookGateway.updateRegisterConfig).mockImplementation(async payload => {
    config = structuredClone(payload)
    delete config.oauth2.client_id
    config.oauth2.clientIdConfigured = true
    status.config = config
    return { config }
  })
  await open()
  expect(wrapper!.text()).toContain('已配置；普通接口不回显')
  expect(input('OAuth Client ID').attributes('type')).toBe('password')
  await input('OAuth Client ID').setValue('CLIENT_ID_FIXTURE')
  await input('OAuth 回调地址').setValue('https://localhost/callback')
  await input('OAuth 权限范围').setValue('offline_access, Mail.Read')
  await button('保存配置').trigger('click')
  await flushPromises()
  const payload = vi.mocked(outlookGateway.updateRegisterConfig).mock.calls[0]![0]
  expect(payload.oauth2).toEqual({
    enable_oauth2: true, client_id: 'CLIENT_ID_FIXTURE',
    redirect_url: 'https://localhost/callback', scopes: ['offline_access', 'Mail.Read'],
  })
  expect((input('OAuth Client ID').element as HTMLInputElement).value).toBe('')
  expect(wrapper!.text()).not.toContain('有未保存的配置')
})
it('retains unsaved settings during polling and refresh', async () => {
  status.enabled = true
  status.status = 'running'
  await open()
  await input('OAuth Client ID').setValue('CLIENT_ID_FIXTURE')
  await input('OAuth 权限范围').setValue('Mail.Read ')
  poll()
  await flushPromises()
  expect(outlookGateway.registerStatus).toHaveBeenCalledTimes(2)
  await button('刷新').trigger('click')
  await flushPromises()
  expect((input('OAuth Client ID').element as HTMLInputElement).value).toBe('CLIENT_ID_FIXTURE')
  expect((input('OAuth 权限范围').element as HTMLTextAreaElement).value).toBe('Mail.Read ')
  expect(wrapper!.text()).toContain('有未保存的配置')
})

it('retains failed saves and prevents starting with unsaved settings', async () => {
  vi.mocked(outlookGateway.updateRegisterConfig).mockRejectedValue(new Error('SAVE_FAILED_FIXTURE'))
  await open()
  await input('OAuth Client ID').setValue('CLIENT_ID_FIXTURE')
  await button('启动').trigger('click')
  expect(outlookGateway.startRegister).not.toHaveBeenCalled()
  await button('保存配置').trigger('click')
  await flushPromises()
  expect((input('OAuth Client ID').element as HTMLInputElement).value).toBe('CLIENT_ID_FIXTURE')
  expect(wrapper!.text()).toContain('有未保存的配置')
})
it('ignores a pre-save poll that arrives after the save response', async () => {
  status.enabled = true
  status.status = 'running'
  await open()
  const stale = structuredClone(status)
  let resolvePoll!: (value: OutlookRegisterSnapshot) => void
  vi.mocked(outlookGateway.registerStatus).mockImplementationOnce(() => new Promise(resolve => { resolvePoll = resolve }))
  poll()
  vi.mocked(outlookGateway.updateRegisterConfig).mockImplementation(async payload => {
    config = structuredClone(payload)
    config.oauth2.clientIdConfigured = true
    status.config = config
    return { config }
  })
  await input('OAuth 权限范围').setValue('Mail.Read')
  await button('保存配置').trigger('click')
  await flushPromises()
  resolvePoll(stale)
  await flushPromises()
  expect((input('OAuth 权限范围').element as HTMLTextAreaElement).value).toBe('Mail.Read')
  expect(wrapper!.text()).not.toContain('有未保存的配置')
})
it('shows Outlook failure categories and actual runtime counters', async () => {
  status.status = 'failed'
  status.error = 'outlook_registration_task_failed'
  status.stats = { submitted: 3, succeeded: 1, failed: 2, running: 0, success_rate: 33.3, elapsed_seconds: 91, batch_index: 2 }
  status.failure_stats = { browser_launch_fail: 2, executor_error: 1 }
  await open()
  const panel = wrapper!.get('[data-testid=outlook-task-status]')
  expect(panel.text()).toContain('执行失败')
  expect(panel.text()).toContain('browser_launch_fail')
  expect(panel.text()).toContain('executor_error')
  expect(panel.get('[data-testid=outlook-metric-submitted]').text()).toBe('3')
  expect(panel.get('[data-testid=outlook-metric-failed]').text()).toBe('2')
  expect(panel.get('[data-testid=outlook-metric-success-rate]').text()).toBe('33.3%')
  expect(panel.get('[data-testid=outlook-metric-elapsed]').text()).toBe('1分31秒')
})

it('polls idle pages so tasks started elsewhere become visible', async () => {
  await open()
  status.enabled = true
  status.status = 'running'
  status.stats = { submitted: 1, running: 1, succeeded: 0, failed: 0 }
  poll()
  await flushPromises()
  expect(outlookGateway.registerStatus).toHaveBeenCalledTimes(2)
  expect(wrapper!.get('[data-testid=outlook-metric-running]').text()).toBe('1')
})

it('shows polling failures without discarding the last snapshot, then recovers', async () => {
  status.stats = { submitted: 2 }
  await open()
  vi.mocked(outlookGateway.registerStatus).mockRejectedValueOnce(new Error('SECRET_FIXTURE_MUST_NOT_RENDER'))
  poll()
  await flushPromises()
  expect(wrapper!.get('[data-testid=outlook-task-stale]').text()).toContain('任务状态刷新失败')
  expect(wrapper!.get('[data-testid=outlook-metric-submitted]').text()).toBe('2')
  expect(wrapper!.text()).not.toContain('SECRET_FIXTURE_MUST_NOT_RENDER')
  expect(button('启动').attributes('disabled')).toBeDefined()
  status.stats = { submitted: 5 }
  poll()
  await flushPromises()
  expect(wrapper!.find('[data-testid=outlook-task-stale]').exists()).toBe(false)
  expect(wrapper!.get('[data-testid=outlook-metric-submitted]').text()).toBe('5')
  expect(button('启动').attributes('disabled')).toBeUndefined()
})

it('coalesces polling and manual refresh while a task read is pending', async () => {
  await open()
  let resolve!: (value: OutlookRegisterSnapshot) => void
  vi.mocked(outlookGateway.registerStatus).mockReturnValueOnce(new Promise(done => { resolve = done }))
  poll()
  poll()
  await button('刷新').trigger('click')
  await flushPromises()
  expect(outlookGateway.registerStatus).toHaveBeenCalledTimes(2)
  resolve({ ...status, stats: { submitted: 8 } })
  await flushPromises()
  expect(wrapper!.get('[data-testid=outlook-metric-submitted]').text()).toBe('8')
  poll()
  await flushPromises()
  expect(outlookGateway.registerStatus).toHaveBeenCalledTimes(3)
})

it.each([
  { label: '启动', method: 'startRegister', before: 'idle', after: 'running', count: 1 },
  { label: '停止', method: 'stopRegister', before: 'running', after: 'stopped', count: 3 },
  { label: '重置', method: 'resetRegister', before: 'running', after: 'idle', count: 0 },
] as const)('ignores pre-action status and logs after $label', async ({ label, method, before, after, count }) => {
  status.status = before
  status.enabled = before === 'running'
  await open()
  let resolve!: (value: OutlookRegisterSnapshot) => void
  vi.mocked(outlookGateway.registerStatus).mockReturnValueOnce(new Promise(done => { resolve = done }))
  vi.mocked(outlookGateway.registerLogs).mockResolvedValueOnce({ items: [{ createdAt: '', level: 'info', line: 'OLD_LOG_FIXTURE' }] })
  poll()
  await flushPromises()
  const result = { ...status, status: after, enabled: after === 'running', stats: { submitted: count } }
  vi.mocked(outlookGateway[method]).mockResolvedValue(result)
  vi.mocked(outlookGateway.registerLogs).mockResolvedValue({ items: [{ createdAt: '', level: 'info', line: 'CURRENT_LOG_FIXTURE' }] })
  await button(label).trigger('click')
  await flushPromises()
  expect(outlookGateway[method]).toHaveBeenCalledTimes(1)
  expect(wrapper!.get('[data-testid=outlook-metric-submitted]').text()).toBe(String(count))
  resolve({ ...status, status: 'running', enabled: true, stats: { submitted: 999 } })
  await flushPromises()
  expect(wrapper!.get('[data-testid=outlook-metric-submitted]').text()).toBe(String(count))
  expect(wrapper!.text()).toContain('CURRENT_LOG_FIXTURE')
  expect(wrapper!.text()).not.toContain('OLD_LOG_FIXTURE')
})

it('keeps the applied action visible when its log refresh fails', async () => {
  status.status = 'running'
  status.enabled = true
  await open()
  vi.mocked(outlookGateway.stopRegister).mockResolvedValue({ ...status, status: 'stopped', enabled: false })
  vi.mocked(outlookGateway.registerLogs).mockRejectedValueOnce(new Error('logs unavailable'))
  await button('停止').trigger('click')
  await flushPromises()
  expect(wrapper!.get('[data-testid=outlook-task-status]').text()).toContain('已停止')
  expect(wrapper!.get('[data-testid=outlook-task-stale]').text()).toContain('任务操作已生效，但日志刷新失败')
})

it('loads task monitoring even when an unrelated account request fails', async () => {
  vi.mocked(outlookGateway.list).mockRejectedValue(new Error('accounts unavailable'))
  status.stats = { submitted: 4 }
  await open()
  expect(wrapper!.get('[data-testid=outlook-metric-submitted]').text()).toBe('4')
  expect(wrapper!.find('[data-testid=outlook-task-stale]').exists()).toBe(false)
})

it('stops polling after unmount and ignores a pending read', async () => {
  await open()
  let resolve!: (value: OutlookRegisterSnapshot) => void
  vi.mocked(outlookGateway.registerStatus).mockReturnValueOnce(new Promise(done => { resolve = done }))
  poll()
  wrapper!.unmount()
  wrapper = undefined
  expect(window.clearInterval).toHaveBeenCalledWith(123)
  resolve(status)
  await flushPromises()
  poll()
  await flushPromises()
  expect(outlookGateway.registerStatus).toHaveBeenCalledTimes(2)
})
