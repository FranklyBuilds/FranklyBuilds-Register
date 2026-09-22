import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { describe, expect, it, vi } from 'vitest'
import ExportDialog from '@/components/ExportDialog.vue'
import AccessTokenGroupsDialog from '@/components/AccessTokenGroupsDialog.vue'
import { dataGateway } from '@/services/dataGateway'
import * as exporter from '@/services/exporter'
import { useAppStore } from '@/stores/app'
import type { AccountRecord, EmailChangeRun, EmailRecord } from '@/types'
import AccountsView from './AccountsView.vue'

function account(overrides: Partial<AccountRecord> = {}): AccountRecord {
  return {
    id: 'account-valid',
    email: 'valid@example.test',
    chatgptPassword: '',
    totpSecret: '',
    emailAccessUrl: 'https://mail.example.test/inbox',
    createdAt: '2026-08-11T00:00:00.000Z',
    accountType: 'free',
    phoneBound: null,
    promotionEligible: null,
    accessTokenConfigured: true,
    accessTokenExpiresAt: '2099-08-11T01:00:00.000Z',
    accessTokenUpdatedAt: '2026-08-11T00:10:00.000Z',
    ...overrides,
  }
}

function targetEmail(overrides: Partial<EmailRecord> = {}): EmailRecord {
  return {
    id: 'target-email',
    email: 'target@example.test',
    accessUrl: 'https://mail.example.test/inbox/target',
    importedAt: '2026-08-11T00:00:00.000Z',
    ...overrides,
  }
}

function emailChangeRun(overrides: Partial<EmailChangeRun> = {}): EmailChangeRun {
  return {
    runId: 'email-change-run',
    kind: 'email_change',
    status: 'target_reserved',
    accountId: 'account-valid',
    oldEmail: 'valid@example.test',
    targetEmailId: 'target-email',
    targetEmail: 'target@example.test',
    stage: 'target_reserved',
    attempts: 0,
    errorCode: null,
    errorMessage: null,
    cancelRequested: false,
    createdAt: '2026-08-11T00:00:00.000Z',
    startedAt: null,
    updatedAt: '2026-08-11T00:00:00.000Z',
    finishedAt: null,
    ...overrides,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

describe('AccountsView AccessToken controls', () => {
  it('labels the registration country and keeps legacy accounts identifiable', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [
      account({ registrationCountry: 'TR' }),
      account({ id: 'legacy-account', email: 'legacy@example.test', registrationCountry: null }),
    ]
    store.accountTotal = 2
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 2,
      page: 1,
      pageSize: 10,
    })

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('土耳其 · TR')
    expect(wrapper.text()).toContain('历史账号')
  })

  it('opens the mailbox URL from the account actions', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [account({ checkoutType: 'oaics' })]
    store.accountTotal = 1
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 1,
      page: 1,
      pageSize: 10,
    })
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('OAICS')
    await wrapper.get('button[aria-label="打开接码 URL"]').trigger('click')
    expect(open).toHaveBeenCalledWith(
      'https://mail.example.test/inbox',
      '_blank',
      'noopener,noreferrer',
    )
    wrapper.unmount()
  })

  it('renders each account action as a compact labeled icon button', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [account()]
    store.accountTotal = 1
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 1,
      page: 1,
      pageSize: 10,
    })

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const actions = wrapper.get('.account-row-actions')
    expect(actions.findAll('button')).toHaveLength(8)
    expect(
      actions.findAll('button').map((button) => button.attributes('aria-label')),
    ).toEqual([
      '账号验活',
      '查询优惠资格',
      '检测账单类型',
      'OAICS 全代理检测',
      '更换邮箱',
      '打开接码 URL',
      '导出账号',
      '提取 Access Token',
    ])
    expect(wrapper.find('.account-actions-column').exists()).toBe(true)
  })

  it('shows AT status and opens a locked single-token export without exposing AT', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [
      account(),
      account({
        id: 'account-missing',
        email: 'missing@example.test',
        accessTokenConfigured: false,
        accessTokenExpiresAt: null,
        accessTokenUpdatedAt: null,
      }),
    ]
    store.accountTotal = 2
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 2,
      page: 1,
      pageSize: 10,
    })

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('AT 状态')
    expect(wrapper.text()).toContain('已提取')
    expect(wrapper.text()).toContain('未提取')
    expect(wrapper.text()).toContain('提取选中 AT')
    expect(wrapper.text()).not.toContain('accessToken')

    const extractButtons = wrapper.findAll('button[aria-label="提取 Access Token"]')
    expect(extractButtons).toHaveLength(2)
    expect(extractButtons[0]!.attributes('disabled')).toBeUndefined()
    expect(extractButtons[1]!.attributes('disabled')).toBeDefined()
    await extractButtons[0]!.trigger('click')

    const dialog = wrapper.getComponent(ExportDialog)
    expect(dialog.props('scope')).toBe('single')
    expect(dialog.props('ids')).toEqual(['account-valid'])
    expect(dialog.props('initialFormat')).toBe('access-tokens')
    expect(dialog.props('formatLocked')).toBe(true)
  })

  it('queries a single account and distinguishes unknown promotion status', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [account()]
    store.accountTotal = 1
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 1,
      page: 1,
      pageSize: 10,
    })
    const query = vi.spyOn(store, 'checkAccountPromotions').mockResolvedValue({
      requested: 1,
      succeeded: 1,
      failed: 0,
      skipped: 0,
      items: [{ id: 'account-valid', status: 'success', errorCode: null }],
    })

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('未查询')
    expect(wrapper.text()).not.toContain('不可试用')
    const button = wrapper.get('button[aria-label="查询优惠资格"]')
    await button.trigger('click')
    await flushPromises()

    expect(query).toHaveBeenCalledWith(['account-valid'])
  })

  it('queries selected accounts through the shared batch action', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [account()]
    store.accountTotal = 1
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 1,
      page: 1,
      pageSize: 10,
    })
    const query = vi.spyOn(store, 'checkAccountPromotions').mockResolvedValue({
      requested: 1,
      succeeded: 1,
      failed: 0,
      skipped: 0,
      items: [{ id: 'account-valid', status: 'success', errorCode: null }],
    })

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()
    wrapper.findComponent({ name: 'ElTable' }).vm.$emit(
      'select',
      [store.accounts[0]],
      store.accounts[0],
    )
    await flushPromises()
    const batchButton = wrapper
      .findAll('button')
      .find((candidate) => candidate.text().includes('查询选中优惠'))
    expect(batchButton).toBeDefined()
    await batchButton!.trigger('click')
    await flushPromises()

    expect(query).toHaveBeenCalledWith(['account-valid'])
  })

  it('filters the complete account pool by the untried Plus label', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [account({ promotionEligible: true })]
    store.accountTotal = 1
    const refresh = vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 1,
      page: 1,
      pageSize: 10,
    })

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const filter = wrapper
      .findAllComponents({ name: 'ElSelect' })
      .find((item) => item.props('placeholder') === '筛选 Plus 标签')
    expect(filter).toBeDefined()
    filter!.vm.$emit('change', 'untried_plus')
    await flushPromises()

    expect(refresh).toHaveBeenLastCalledWith(expect.objectContaining({
      promotion: 'untried_plus',
    }))
  })

  it('opens the ten-per-group AT copier from the account toolbar', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [account()]
    store.accountTotal = 1
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 1,
      page: 1,
      pageSize: 10,
    })
    const exportAll = vi.spyOn(dataGateway, 'exportAccounts').mockResolvedValue({
      content: 'AT_ONE\nAT_TWO',
      filename: 'accounts-2-access-tokens.txt',
      count: 2,
      format: 'access-tokens',
      skippedMissingCount: 0,
      skippedExpiredCount: 0,
    })

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const groupButton = wrapper
      .findAll('button')
      .find((candidate) => candidate.text().includes('AT 分组复制'))
    expect(groupButton).toBeDefined()
    await groupButton!.trigger('click')
    await flushPromises()

    expect(wrapper.getComponent(AccessTokenGroupsDialog).props('modelValue')).toBe(true)
    expect(exportAll).toHaveBeenCalledWith('access-tokens', 'all', [])
  })

  it('copies the configured number of valid ATs with one click', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [account()]
    store.accountTotal = 120
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 120,
      page: 1,
      pageSize: 10,
    })
    const tokens = Array.from({ length: 120 }, (_, index) => `AT_${index + 1}`)
    vi.spyOn(dataGateway, 'exportAccounts').mockResolvedValue({
      content: tokens.join('\n'),
      filename: 'accounts-120-access-tokens.txt',
      count: 120,
      format: 'access-tokens',
      skippedMissingCount: 0,
      skippedExpiredCount: 0,
    })
    const copy = vi.spyOn(exporter, 'copyText').mockResolvedValue(undefined)

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const limitInput = wrapper.findAllComponents({ name: 'ElInputNumber' }).at(-1)
    expect(limitInput).toBeDefined()
    limitInput!.vm.$emit('update:modelValue', 37)
    await flushPromises()

    const button = wrapper
      .findAll('button')
      .find((candidate) => candidate.text().includes('一键复制前 37 个 AT'))
    expect(button).toBeDefined()
    await button!.trigger('click')
    await flushPromises()

    expect(dataGateway.exportAccounts).toHaveBeenCalledWith('access-tokens', 'all', [])
    expect(copy).toHaveBeenCalledWith(tokens.slice(0, 37).join('\n'))
  })

  it('starts an email change with an available target and refreshes after completion', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [account()]
    store.accountTotal = 1
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 1,
      page: 1,
      pageSize: 10,
    })
    vi.spyOn(dataGateway, 'listEmails').mockImplementation(async ({ page }) => ({
      items: page === 1
        ? [targetEmail(), targetEmail({ id: 'same-email', email: 'VALID@example.test' })]
        : [targetEmail({ id: 'second-page', email: 'second-page@example.test' })],
      total: 3,
      page,
      pageSize: 100,
    }))
    const create = vi.spyOn(dataGateway, 'createEmailChange').mockResolvedValue(emailChangeRun())
    const get = vi.spyOn(dataGateway, 'getEmailChange').mockResolvedValue(
      emailChangeRun({ status: 'completed', stage: 'completed', finishedAt: '2026-08-11T00:01:00.000Z' }),
    )

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const button = wrapper.get('button[aria-label="更换邮箱"]')
    await button.trigger('click')
    await flushPromises()

    expect(dataGateway.listEmails).toHaveBeenCalledWith({ page: 1, pageSize: 100, q: '', source: 'all' })
    expect(dataGateway.listEmails).toHaveBeenCalledWith({ page: 2, pageSize: 100, q: '', source: 'all' })
    await wrapper.get('.email-change-target').trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain('target@example.test')
    expect(document.body.textContent).toContain('second-page@example.test')
    expect(document.body.textContent).not.toContain('VALID@example.test')

    const select = wrapper
      .findAllComponents({ name: 'ElSelect' })
      .find((item) => item.classes().includes('email-change-target'))
    expect(select).toBeDefined()
    select!.vm.$emit('update:modelValue', 'target-email')
    await flushPromises()
    const submit = wrapper.findAll('button').find((candidate) => candidate.text().includes('开始换绑'))
    expect(submit).toBeDefined()
    await submit!.trigger('click')
    await flushPromises()

    expect(create).toHaveBeenCalledWith('account-valid', 'target-email')
    await new Promise((resolve) => setTimeout(resolve, 1600))
    await flushPromises()
    expect(get).toHaveBeenCalledWith('email-change-run')
    expect(store.refreshAccounts).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('disables bulk deletion while the selected account has an email-change request', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [account()]
    store.accountTotal = 1
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 1,
      page: 1,
      pageSize: 10,
    })

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()
    wrapper.findComponent({ name: 'ElTable' }).vm.$emit(
      'select',
      [store.accounts[0]],
      store.accounts[0],
    )
    wrapper.getComponent({ name: 'EmailChangeDialog' }).vm.$emit('active-change', 'account-valid')
    await flushPromises()

    const deleteButton = wrapper
      .findAll('button')
      .find((candidate) => candidate.text().includes('删除选中'))
    expect(deleteButton).toBeDefined()
    expect(deleteButton!.attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('does not let a completed refresh close a newer account dialog', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    const accounts = [
      account(),
      account({ id: 'account-two', email: 'two@example.test' }),
    ]
    const page = { items: accounts, total: 2, page: 1, pageSize: 10 as const }
    const completionRefresh = deferred<typeof page>()
    store.accounts = accounts
    store.accountTotal = accounts.length
    vi.spyOn(store, 'refreshAccounts')
      .mockResolvedValueOnce(page)
      .mockReturnValueOnce(completionRefresh.promise)
    vi.spyOn(store, 'refreshEmails').mockResolvedValue({
      items: [], total: 0, page: 1, pageSize: 10,
    })
    vi.spyOn(store, 'refreshStats').mockResolvedValue(store.stats)
    vi.spyOn(dataGateway, 'listEmails').mockResolvedValue({
      items: [targetEmail()], total: 1, page: 1, pageSize: 100,
    })

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const changeButtons = wrapper.findAll('button[aria-label="更换邮箱"]')
    await changeButtons[0]!.trigger('click')
    await flushPromises()
    const dialog = wrapper.getComponent({ name: 'EmailChangeDialog' })
    dialog.vm.$emit('active-change', null)
    dialog.vm.$emit('completed', emailChangeRun({ status: 'completed', stage: 'completed' }))
    await flushPromises()

    await wrapper.findAll('button[aria-label="更换邮箱"]')[1]!.trigger('click')
    await flushPromises()
    expect(dialog.props('modelValue')).toBe(true)
    expect((dialog.props('account') as AccountRecord).id).toBe('account-two')

    completionRefresh.resolve(page)
    await flushPromises()
    expect(dialog.props('modelValue')).toBe(true)
    expect((dialog.props('account') as AccountRecord).id).toBe('account-two')
    wrapper.unmount()
  })
})
