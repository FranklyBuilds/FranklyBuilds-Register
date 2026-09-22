import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { dataGateway } from '@/services/dataGateway'
import * as exporter from '@/services/exporter'
import ExportDialog from './ExportDialog.vue'

afterEach(() => vi.restoreAllMocks())

describe('ExportDialog account formats', () => {
  it('offers both password and mailbox URL combinations', async () => {
    const wrapper = mount(ExportDialog, {
      props: {
        modelValue: true,
        scope: 'all',
        ids: [],
        count: 2,
      },
      global: { plugins: [ElementPlus] },
    })

    await flushPromises()
    expect(wrapper.find('input[value="credentials"]').exists()).toBe(true)
    expect(wrapper.find('input[value="credentials-mail-links-totp"]').exists()).toBe(true)
    expect(wrapper.find('input[value="credentials-mail-links"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('账号----密码----接码 URL----2FA')
    expect(wrapper.text()).toContain('账号----密码----接码 URL')
  })
})

describe('ExportDialog access token export', () => {
  it('downloads only the server-provided AT text and reports skipped rows', async () => {
    vi.spyOn(dataGateway, 'exportAccounts').mockResolvedValue({
      content: 'AT_ONE\nAT_TWO',
      filename: 'accounts-2-access-tokens-20260811-010000.txt',
      count: 2,
      format: 'access-tokens',
      skippedMissingCount: 1,
      skippedExpiredCount: 1,
    })
    const download = vi.spyOn(exporter, 'downloadTextFile').mockImplementation(() => undefined)
    const wrapper = mount(ExportDialog, {
      props: {
        modelValue: true,
        scope: 'selected',
        ids: ['one', 'two', 'missing', 'expired'],
        count: 4,
        initialFormat: 'access-tokens',
        formatLocked: true,
      },
      global: { plugins: [ElementPlus] },
    })

    await (wrapper.vm as unknown as { confirmExport: () => Promise<void> }).confirmExport()
    await flushPromises()

    expect(dataGateway.exportAccounts).toHaveBeenCalledWith(
      'access-tokens',
      'selected',
      ['one', 'two', 'missing', 'expired'],
    )
    expect(download).toHaveBeenCalledWith(
      'AT_ONE\nAT_TWO',
      'accounts-2-access-tokens-20260811-010000.txt',
    )
  })

  it('does not create an empty file when every selected account is skipped', async () => {
    vi.spyOn(dataGateway, 'exportAccounts').mockResolvedValue({
      content: '',
      filename: 'accounts-0-access-tokens-20260811-010000.txt',
      count: 0,
      format: 'access-tokens',
      skippedMissingCount: 2,
      skippedExpiredCount: 1,
    })
    const download = vi.spyOn(exporter, 'downloadTextFile').mockImplementation(() => undefined)
    const copy = vi.spyOn(exporter, 'copyText').mockResolvedValue(undefined)
    const wrapper = mount(ExportDialog, {
      props: {
        modelValue: true,
        scope: 'selected',
        ids: ['one', 'two', 'three'],
        count: 3,
        initialFormat: 'access-tokens',
        formatLocked: true,
      },
      global: { plugins: [ElementPlus] },
    })

    await (wrapper.vm as unknown as { confirmExport: () => Promise<void> }).confirmExport()
    await flushPromises()

    expect(download).not.toHaveBeenCalled()
    expect(copy).not.toHaveBeenCalled()
  })
})
