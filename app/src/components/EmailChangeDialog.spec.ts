import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it, vi } from 'vitest'
import { dataGateway } from '@/services/dataGateway'
import type { AccountRecord, EmailChangeRun, EmailRecord } from '@/types'
import EmailChangeDialog from './EmailChangeDialog.vue'

function account(overrides: Partial<AccountRecord> = {}): AccountRecord {
  return {
    id: 'account-one',
    email: 'one@example.test',
    chatgptPassword: '',
    totpSecret: '',
    emailAccessUrl: 'https://mail.example.test/one',
    createdAt: '2026-08-11T00:00:00.000Z',
    accountType: 'free',
    phoneBound: null,
    promotionEligible: null,
    accessTokenConfigured: true,
    accessTokenExpiresAt: '2099-08-11T00:00:00.000Z',
    accessTokenUpdatedAt: '2026-08-11T00:00:00.000Z',
    ...overrides,
  }
}

function target(overrides: Partial<EmailRecord> = {}): EmailRecord {
  return {
    id: 'target-one',
    email: 'target@example.test',
    accessUrl: 'https://mail.example.test/target',
    importedAt: '2026-08-11T00:00:00.000Z',
    ...overrides,
  }
}

function run(overrides: Partial<EmailChangeRun> = {}): EmailChangeRun {
  return {
    runId: 'run-one',
    kind: 'email_change',
    status: 'target_reserved',
    accountId: 'account-one',
    oldEmail: 'one@example.test',
    targetEmailId: 'target-one',
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
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

describe('EmailChangeDialog request lifecycle', () => {
  it('starts one target load when the account and open state arrive together', async () => {
    const listEmails = vi.spyOn(dataGateway, 'listEmails').mockResolvedValue({
      items: [target()],
      total: 1,
      page: 1,
      pageSize: 100,
    })
    const wrapper = mount(EmailChangeDialog, {
      props: { modelValue: false, account: null },
      global: { plugins: [ElementPlus] },
    })

    await wrapper.setProps({ modelValue: true, account: account() })
    await flushPromises()

    expect(listEmails).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('ignores target-mail responses from a previous account', async () => {
    type EmailPage = Awaited<ReturnType<typeof dataGateway.listEmails>>
    const firstPage = deferred<EmailPage>()
    const secondPage = deferred<EmailPage>()
    vi.spyOn(dataGateway, 'listEmails')
      .mockReturnValueOnce(firstPage.promise)
      .mockReturnValueOnce(secondPage.promise)
    const wrapper = mount(EmailChangeDialog, {
      props: { modelValue: true, account: account() },
      global: { plugins: [ElementPlus] },
    })

    await wrapper.setProps({ account: account({ id: 'account-two', email: 'two@example.test' }) })
    secondPage.resolve({
      items: [target({ id: 'target-two', email: 'second-target@example.test' })],
      total: 1,
      page: 1,
      pageSize: 100,
    })
    firstPage.resolve({ items: [target()], total: 1, page: 1, pageSize: 100 })
    await flushPromises()

    const labels = wrapper.findAllComponents({ name: 'ElOption' }).map((option) => option.props('label'))
    expect(labels).toEqual(['second-target@example.test'])
    expect(wrapper.emitted('active-change')).not.toContainEqual(['account-one'])
    wrapper.unmount()
  })

  it('ignores a submit response after the account context changes', async () => {
    vi.spyOn(dataGateway, 'listEmails').mockResolvedValue({
      items: [target()],
      total: 1,
      page: 1,
      pageSize: 100,
    })
    const create = deferred<EmailChangeRun>()
    vi.spyOn(dataGateway, 'createEmailChange').mockReturnValue(create.promise)
    const wrapper = mount(EmailChangeDialog, {
      props: { modelValue: true, account: account() },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    const select = wrapper.findComponent({ name: 'ElSelect' })
    select.vm.$emit('update:modelValue', 'target-one')
    await flushPromises()
    const submit = wrapper.findAll('button').find((button) => button.text().includes('开始换绑'))
    expect(submit).toBeDefined()
    await submit!.trigger('click')
    await wrapper.setProps({ account: account({ id: 'account-two', email: 'two@example.test' }) })

    create.resolve(run())
    await flushPromises()

    expect(wrapper.text()).not.toContain('目标邮箱已预留')
    // The account is reserved as soon as the POST starts; the stale response
    // must still avoid writing a run or re-activating the old account.
    expect(wrapper.emitted('active-change')).toContainEqual(['account-one'])
    expect(wrapper.text()).not.toContain('正在提交远端换绑')
    wrapper.unmount()
  })

  it('keeps the dialog open when a close event arrives during submit', async () => {
    vi.spyOn(dataGateway, 'listEmails').mockResolvedValue({
      items: [target()],
      total: 1,
      page: 1,
      pageSize: 100,
    })
    const create = deferred<EmailChangeRun>()
    vi.spyOn(dataGateway, 'createEmailChange').mockReturnValue(create.promise)
    const wrapper = mount(EmailChangeDialog, {
      props: { modelValue: true, account: account() },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    wrapper.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'target-one')
    await flushPromises()
    const submit = wrapper.findAll('button').find((button) => button.text().includes('开始换绑'))
    expect(submit).toBeDefined()
    await submit!.trigger('click')
    wrapper.findComponent({ name: 'ElDialog' }).vm.$emit('update:modelValue', false)
    await flushPromises()

    expect(wrapper.emitted('update:modelValue')).toContainEqual([true])
    expect(wrapper.emitted('update:modelValue')).not.toContainEqual([false])
    create.resolve(run({ status: 'failed', stage: 'failed' }))
    await flushPromises()
    wrapper.unmount()
  })

  it('preserves the pending submit when a parent briefly sets modelValue false', async () => {
    vi.spyOn(dataGateway, 'listEmails').mockResolvedValue({
      items: [target()],
      total: 1,
      page: 1,
      pageSize: 100,
    })
    const create = deferred<EmailChangeRun>()
    vi.spyOn(dataGateway, 'createEmailChange').mockReturnValue(create.promise)
    const wrapper = mount(EmailChangeDialog, {
      props: { modelValue: true, account: account() },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    wrapper.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'target-one')
    await flushPromises()
    const submit = wrapper.findAll('button').find((button) => button.text().includes('开始换绑'))
    expect(submit).toBeDefined()
    await submit!.trigger('click')

    await wrapper.setProps({ modelValue: false })
    expect(wrapper.emitted('update:modelValue')).toContainEqual([true])
    await wrapper.setProps({ modelValue: true })
    create.resolve(run())
    await flushPromises()

    expect(wrapper.text()).toContain('目标邮箱已预留')
    wrapper.unmount()
  })

  it('does not write or start a run after the component is unmounted', async () => {
    vi.spyOn(dataGateway, 'listEmails').mockResolvedValue({
      items: [target()],
      total: 1,
      page: 1,
      pageSize: 100,
    })
    const create = deferred<EmailChangeRun>()
    const get = vi.spyOn(dataGateway, 'getEmailChange').mockResolvedValue(run())
    vi.spyOn(dataGateway, 'createEmailChange').mockReturnValue(create.promise)
    const wrapper = mount(EmailChangeDialog, {
      props: { modelValue: true, account: account() },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    wrapper.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'target-one')
    await flushPromises()
    const submit = wrapper.findAll('button').find((button) => button.text().includes('开始换绑'))
    expect(submit).toBeDefined()
    await submit!.trigger('click')
    wrapper.unmount()
    create.resolve(run())
    await flushPromises()

    expect(get).not.toHaveBeenCalled()
  })

  it('drops the old submit when the parent swaps account and closes together', async () => {
    vi.spyOn(dataGateway, 'listEmails').mockResolvedValue({
      items: [target({ id: 'target-two', email: 'target-two@example.test' })],
      total: 1,
      page: 1,
      pageSize: 100,
    })
    const create = deferred<EmailChangeRun>()
    vi.spyOn(dataGateway, 'createEmailChange').mockReturnValue(create.promise)
    const wrapper = mount(EmailChangeDialog, {
      props: { modelValue: true, account: account() },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    wrapper.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'target-two')
    await flushPromises()
    const submit = wrapper.findAll('button').find((button) => button.text().includes('开始换绑'))
    expect(submit).toBeDefined()
    await submit!.trigger('click')

    await wrapper.setProps({
      modelValue: false,
      account: account({ id: 'account-two', email: 'two@example.test' }),
    })
    await wrapper.setProps({ modelValue: true })
    create.resolve(run())
    await flushPromises()

    expect(wrapper.text()).not.toContain('目标邮箱已预留')
    expect(wrapper.text()).toContain('two@example.test')
    wrapper.unmount()
  })
})
