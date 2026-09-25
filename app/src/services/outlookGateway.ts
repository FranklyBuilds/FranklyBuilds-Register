import type { EmailRecord, ProxyGroupSummary } from '@/types'

export interface OutlookAccount {
  id: string
  email: string
  oauthStatus: string
  oauthCheckedAt: string | null
  graphStatus: string
  graphCheckedAt: string | null
  lastError: string | null
  poolStatus: 'available' | 'reserved' | 'assigned' | 'unavailable' | 'conflict' | 'not_published'
  outlookEmailId: string | null
  importedAt: string
  source: string
  hasClientId: boolean
  hasRefreshToken: boolean
}

export interface OutlookMessage {
  id: string
  subject: string
  from: string
  fromName: string
  receivedAt: string
  isRead: boolean
  preview: string
  hasAttachments: boolean
}

export interface OutlookImportResult { total: number; imported: number; duplicates: number; errors: number }
export interface OutlookExportInput { ids?: string[] | null }
export interface OutlookProxy {
  id: string; host: string; port: number; enabled: boolean; status: string
  latencyMs: number | null; country: string; group: string; scheme: string
}
export interface OutlookRegisterStats {
  submitted?: number; succeeded?: number; failed?: number; running?: number
  status?: string; tasks?: number; success_tasks?: number | null
}
export interface OutlookOAuthConfig {
  enable_oauth2: boolean; redirect_url: string; scopes: string[] | string
  client_id?: string; clientIdConfigured?: boolean
}

export interface OutlookRegisterConfig {
  execution_mode: 'auto' | 'registration' | 'authorized' | 'both'
  tasks: number; concurrent_flows: number; headless: boolean
  proxy: { source: string; mode: string; group: string; [key: string]: unknown }
  oauth2: OutlookOAuthConfig
  temp_mail?: Record<string, unknown>
  [key: string]: unknown
}

export interface OutlookRegisterSnapshot {
  taskId: string; enabled: boolean; status: string; config: OutlookRegisterConfig; stats: OutlookRegisterStats
  failure_stats?: Record<string, number>; result_count?: number; log_count?: number; proxyGroup?: string; proxyCount?: number
}

export interface OutlookPoolStats {
  total: number; registered: number; oauth2: number; recovery: number; sub: number
  oauth_ok: number; oauth_bad: number; oauth_unknown: number
}

export interface OutlookPoolItem {
  id: string; category: 'registered' | 'oauth2' | 'recovery' | 'sub' | string
  email: string; parentId?: string; parentEmail?: string; tag?: string
  passwordConfigured?: boolean; hasClientId?: boolean; hasRefreshToken?: boolean
  oauthStatus: string; graphStatus?: string; oauthStatusBucket?: string
  poolStatus?: string; status?: string; source?: string; recoveryEmailConfigured?: boolean
  recoveryEmail?: string | null; receiveUrl?: string | null; receiveUiUrl?: string | null
  createdAt?: string; updatedAt?: string; lastError?: string | null
}

export interface OutlookPoolCheckConfig {
  enabled: boolean; interval_sec: number; delay_ms: number
  last_run_at?: string | null; last_result?: Record<string, any> | null; running?: boolean
}

export interface OutlookSubEmailResult {
  account_id?: string; parent_email?: string; created: OutlookPoolItem[]; sub_count?: number
}

export interface OutlookBatchSubEmailResult {
  ok: boolean; success: number; failed: number; results: OutlookSubEmailResult[]; errors: Array<{ id: string; error: string }>
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { ...(init.body ? { 'Content-Type': 'application/json' } : {}), ...(init.headers || {}) },
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
  list: (query: { page?: number; pageSize?: number; q?: string; source?: string; poolStatus?: string } = {}) => {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== '') params.set(key, String(value))
    return request<{ items: OutlookAccount[]; total: number; page: number; pageSize: number }>(`/api/outlook/accounts?${params}`)
  },
  update: (id: string, changes: { password?: string; clientId?: string; refreshToken?: string }) => request<{ account: OutlookAccount }>(`/api/outlook/accounts/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(changes) }),
  remove: (id: string) => request<{ deleted: number }>(`/api/outlook/accounts/${encodeURIComponent(id)}/delete`, { method: 'POST' }),
  results: (limit = 100, q = '') => request<Array<{ email: string; oauthStatus: string; graphStatus: string; createdAt: string }>>(`/api/outlook/results?${new URLSearchParams({ limit: String(limit), q })}`),
  proxies: (page = 1, pageSize = 100) => request<{ items: OutlookProxy[]; total: number; page: number; pageSize: number }>(`/api/outlook/proxies?page=${page}&pageSize=${pageSize}`),
  proxyGroups: () => request<ProxyGroupSummary[]>('/api/outlook/proxy-groups'),
  registerStatus: () => request<OutlookRegisterSnapshot>('/api/outlook/register'),
  updateRegisterConfig: (config: OutlookRegisterConfig) => request<{ config: OutlookRegisterConfig }>('/api/outlook/register', { method: 'PUT', body: JSON.stringify(config) }),
  startRegister: () => request<OutlookRegisterSnapshot>('/api/outlook/register/start', { method: 'POST' }),
  stopRegister: () => request<OutlookRegisterSnapshot>('/api/outlook/register/stop', { method: 'POST' }),
  resetRegister: () => request<OutlookRegisterSnapshot>('/api/outlook/register/reset', { method: 'POST' }),
  registerLogs: (limit = 200) => request<{ items: Array<{ createdAt: string; level: string; line: string }> }>(`/api/outlook/register/logs?limit=${limit}`),
  import: (accounts: Array<{ email: string; password?: string; clientId?: string; refreshToken?: string }>) => request<OutlookImportResult>('/api/outlook/import', { method: 'POST', body: JSON.stringify({ accounts }) }),
  migration: () => request<{ summary?: Record<string, unknown> | null; lastRunAt?: string | null; error?: string }>('/api/outlook/migration'),
  migrateLegacy: () => request<Record<string, unknown>>('/api/outlook/import-legacy', { method: 'POST' }),
  checkOauth: (id: string) => request<{ ok: boolean; oauthStatus: string; graphStatus: string; poolStatus: string; error?: string }>(`/api/outlook/accounts/${encodeURIComponent(id)}/check-oauth`, { method: 'POST' }),
  checkGraph: (id: string) => request<{ ok: boolean; oauthStatus: string; graphStatus: string; poolStatus: string; error?: string }>(`/api/outlook/accounts/${encodeURIComponent(id)}/check-graph`, { method: 'POST' }),
  messages: (id: string, top = 20) => request<{ email: string; messages: OutlookMessage[] }>(`/api/outlook/accounts/${encodeURIComponent(id)}/messages?top=${top}`),
  message: (id: string, messageId: string) => request<{ email: string; message: OutlookMessage & { body: string; bodyType: string } }>(`/api/outlook/accounts/${encodeURIComponent(id)}/messages/${encodeURIComponent(messageId)}`),
  export: (ids?: string[]) => request<string>('/api/outlook/export', { method: 'POST', body: JSON.stringify({ ids: ids?.length ? ids : null }) }),
  poolStats: () => request<{ stats: OutlookPoolStats }>('/api/outlook/pool/stats'),
  poolAccounts: (query: { category?: string; keyword?: string; country?: string; age?: string; oauth_status?: string; page?: number; page_size?: number } = {}) => {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== '') params.set(key, String(value))
    return request<{ items: OutlookPoolItem[]; total: number; page: number; page_size: number }>(`/api/outlook/pool/accounts?${params}`)
  },
  generateSubEmails: (accountId: string, count = 1, tagPrefix = '') => request<OutlookSubEmailResult>(`/api/outlook/pool/accounts/${encodeURIComponent(accountId)}/sub-emails`, { method: 'POST', body: JSON.stringify({ count, tag_prefix: tagPrefix }) }),
  batchGenerateSubEmails: (ids: string[], count = 1, tagPrefix = '') => request<OutlookBatchSubEmailResult>('/api/outlook/pool/batch/sub-emails', { method: 'POST', body: JSON.stringify({ ids, count, tag_prefix: tagPrefix }) }),
  batchCheckPoolOauth: (ids: string[]) => request<Record<string, any>>('/api/outlook/pool/batch/check-oauth', { method: 'POST', body: JSON.stringify({ ids }) }),
  deletePoolItems: (ids: string[]) => request<{ deleted: number }>('/api/outlook/pool/accounts', { method: 'DELETE', body: JSON.stringify({ ids }) }),
  exportPool: (category: string, ids?: string[], country = '') => request<string>('/api/outlook/pool/export', { method: 'POST', body: JSON.stringify({ category, ids: ids?.length ? ids : null, country: country || null }) }),
  oauthCheckConfig: () => request<{ config: OutlookPoolCheckConfig }>('/api/outlook/pool/config/oauth-check'),
  updateOauthCheckConfig: (config: Partial<OutlookPoolCheckConfig>) => request<{ ok: boolean; config: OutlookPoolCheckConfig }>('/api/outlook/pool/config/oauth-check', { method: 'PUT', body: JSON.stringify(config) }),
  receive: (token: string, top = 10) => request<{ ok: boolean; email: string; latest_code?: string | null; codes?: string[]; messages: OutlookMessage[]; receive_ui_url?: string }>(`/api/outlook/pool/receive/${encodeURIComponent(token)}?top=${top}`),
}

export function asOutlookEmail(row: OutlookAccount): Pick<EmailRecord, 'sourceType' | 'outlookAccountId'> {
  return { sourceType: 'outlook', outlookAccountId: row.id }
}
