export interface OutlookRegisterConfig {
  email_suffix?: string
  headless?: boolean
  bot_protection_wait?: number
  max_captcha_retries?: number
  captcha_strategy?: number
  concurrent_flows?: number
  tasks?: number
  success_tasks?: number | null
  batch_success_limit?: number
  proxy?: {
    mode?: 'single' | 'multiple'
    type?: 'http' | 'https' | 'socks5' | 'socks5h'
    host?: string
    single_port?: number
    port_start?: number
    port_end?: number
    max_per_proxy?: number
  }
  oauth2?: Record<string, unknown>
  temp_mail?: Record<string, unknown>
}

export interface OutlookRegisterState extends OutlookRegisterConfig {
  status?: string
  stats?: Record<string, unknown>
  logs?: Array<{ ts?: string; level?: string; line?: string }>
  running?: boolean
}

const BASE_KEY = 'outlookRegister.apiBase'
const TOKEN_KEY = 'outlookRegister.adminToken'

export function getOutlookBaseUrl() {
  try {
    return localStorage.getItem(BASE_KEY) || 'http://127.0.0.1:8001'
  } catch {
    return 'http://127.0.0.1:8001'
  }
}

export function setOutlookConnection(baseUrl: string, token: string) {
  localStorage.setItem(BASE_KEY, baseUrl.trim().replace(/\/$/, ''))
  localStorage.setItem(TOKEN_KEY, token.trim())
}

function headers(json = false) {
  const result: Record<string, string> = {}
  if (json) result['Content-Type'] = 'application/json'
  try {
    const token = localStorage.getItem(TOKEN_KEY) || ''
    if (token) result.Authorization = `Bearer ${token}`
  } catch {
    // localStorage is optional in non-browser tests.
  }
  return result
}

async function request<T>(path: string, init: RequestInit = {}) {
  const response = await fetch(`${getOutlookBaseUrl()}${path}`, {
    ...init,
    headers: { ...headers(Boolean(init.body)), ...(init.headers || {}) },
  })
  const raw = await response.text()
  let body: any = null
  try { body = raw ? JSON.parse(raw) : null } catch { body = raw }
  if (!response.ok) {
    const detail = body?.detail || body?.error || response.statusText
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return body as T
}

export const outlookGateway = {
  health: () => request<{ ok: boolean; service?: string }>('/api/health'),
  getRegister: () => request<OutlookRegisterState>('/api/register'),
  saveRegister: (config: OutlookRegisterConfig) => request<OutlookRegisterState>('/api/register', {
    method: 'POST', body: JSON.stringify(config),
  }),
  start: () => request<OutlookRegisterState>('/api/register/start', { method: 'POST' }),
  stop: () => request<OutlookRegisterState>('/api/register/stop', { method: 'POST' }),
  reset: () => request<OutlookRegisterState>('/api/register/reset', { method: 'POST' }),
}
