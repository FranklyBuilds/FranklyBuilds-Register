import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { outlookGateway, type OutlookRegisterConfig, type OutlookRegisterSnapshot } from '@/services/outlookGateway'
import OutlookRegisterView from './OutlookRegisterView.vue'

vi.mock('@/services/outlookGateway', () => ({ outlookGateway: {
  list: vi.fn(), migration: vi.fn(), results: vi.fn(), proxies: vi.fn(), proxyGroups: vi.fn(),
  registerStatus: vi.fn(), registerLogs: vi.fn(), poolStats: vi.fn(), poolAccounts: vi.fn(),
  oauthCheckConfig: vi.fn(), updateRegisterConfig: vi.fn(), startRegister: vi.fn(),
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
