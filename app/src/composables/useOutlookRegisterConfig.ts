import { ref, watch } from 'vue'
import type { OutlookRegisterConfig } from '@/services/outlookGateway'

export function useOutlookRegisterConfig() {
  const config = ref<OutlookRegisterConfig>({
    execution_mode: 'auto', tasks: 1, concurrent_flows: 1, headless: false,
    proxy: { source: 'mongo', mode: 'mongo', group: '', max_per_proxy: 20 },
    oauth2: { enable_oauth2: true, redirect_url: 'https://localhost', scopes: [], client_id: '' },
  })
  const dirty = ref(false)
  let applying = false
  let revision = 0
  watch(config, () => { if (!applying) { dirty.value = true; revision += 1 } }, { deep: true, flush: 'sync' })

  function accept(value?: Partial<OutlookRegisterConfig>, force = false, expectedRevision = revision) {
    if (!value || (!force && (dirty.value || expectedRevision !== revision))) return
    const incoming = JSON.parse(JSON.stringify(value)) as Partial<OutlookRegisterConfig>
    applying = true
    try {
      config.value = {
        ...config.value, ...incoming,
        proxy: { ...config.value.proxy, ...incoming.proxy },
        oauth2: { ...config.value.oauth2, ...incoming.oauth2, client_id: '' },
      }
      if (force) revision += 1
      dirty.value = false
    } finally { applying = false }
  }

  function payload(): OutlookRegisterConfig {
    const result = JSON.parse(JSON.stringify(config.value)) as OutlookRegisterConfig
    if (typeof result.oauth2.scopes === 'string') {
      result.oauth2.scopes = result.oauth2.scopes.split(/[\s,]+/).filter(Boolean)
    }
    if (!result.oauth2.client_id?.trim()) delete result.oauth2.client_id
    delete result.oauth2.clientIdConfigured
    if (result.temp_mail) delete result.temp_mail.adminPasswordConfigured
    return result
  }

  return { config, dirty, accept, payload, generation: () => revision }
}
