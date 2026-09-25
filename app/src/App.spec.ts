import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { describe, expect, it, vi } from 'vitest'
import App from './App.vue'
import router from './router'
import { useAppStore } from './stores/app'

describe('App navigation', () => {
  it('shows the FranklyBuilds register branding', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    vi.spyOn(store, 'bootstrap').mockResolvedValue(undefined)
    await router.push('/launch')
    await router.isReady()

    const wrapper = mount(App, {
      global: {
        plugins: [pinia, router, ElementPlus],
        stubs: { RouterView: true },
      },
    })

    expect(wrapper.find('.brand-copy strong').text()).toBe('FranklyBuilds-Register')
    expect(wrapper.find('.brand-copy span').text()).toContain('FB注册机')
    wrapper.unmount()
  })

  it('shows registration and payment pages in navigation', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    vi.spyOn(store, 'bootstrap').mockResolvedValue(undefined)
    await router.push('/launch')
    await router.isReady()

    const wrapper = mount(App, {
      global: {
        plugins: [pinia, router, ElementPlus],
        stubs: { RouterView: true },
      },
    })

    const labels = wrapper.findAll('.nav-menu .el-menu-item').map((item) => item.text())
    expect(labels).toEqual(['启动界面', '配置栏', '账号池', '邮箱池', '提炼', '自动流水线', 'HeroSMS 接码', '成品管理', '协议授权', 'Outlook 账号', 'MailCom 管理'])
    expect(router.resolve('/settings').name).toBe('settings')
    expect(router.resolve('/payment-tools').name).toBe('payment-tools')
    expect(router.resolve('/pipeline').name).toBe('pipeline')
    expect(router.resolve('/hero-sms').name).toBe('hero-sms')
    expect(router.resolve('/paid-accounts').name).toBe('paid-accounts')
    expect(router.resolve('/agreement-tools').name).toBe('agreement-tools')
    wrapper.unmount()
  })
})
