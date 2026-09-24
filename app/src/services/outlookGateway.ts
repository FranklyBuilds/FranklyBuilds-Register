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
export interface OutlookRegisterSnapshot {
  enabled: boolean; config: Record<string, any>; stats: OutlookRegisterStats
  failure_stats?: Record<string, number>; result_count?: number
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
  import: (accounts: Array<{ email: string; password?: string; clientId?: string; refreshToken?: string }>) => request<OutlookImportResult>('/api/outlook/import', { method: 'POST', body: JSON.stringify({ accounts }) }),
  migration: () => request<{ summary?: Record<string, unknown> | null; lastRunAt?: string | null; error?: string }>('/api/outlook/migration'),
  migrateLegacy: () => request<Record<string, unknown>>('/api/outlook/import-legacy', { method: 'POST' }),
  checkOauth: (id: string) => request<{ ok: boolean; oauthStatus: string; graphStatus: string; poolStatus: string; error?: string }>(`/api/outlook/accounts/${encodeURIComponent(id)}/check-oauth`, { method: 'POST' }),
  checkGraph: (id: string) => request<{ ok: boolean; oauthStatus: string; graphStatus: string; poolStatus: string; error?: string }>(`/api/outlook/accounts/${encodeURIComponent(id)}/check-graph`, { method: 'POST' }),
  messages: (id: string, top = 20) => request<{ email: string; messages: OutlookMessage[] }>(`/api/outlook/accounts/${encodeURIComponent(id)}/messages?top=${top}`),
  message: (id: string, messageId: string) => request<{ email: string; message: OutlookMessage & { body: string; bodyType: string } }>(`/api/outlook/accounts/${encodeURIComponent(id)}/messages/${encodeURIComponent(messageId)}`),
  export: (ids?: string[]) => request<string>('/api/outlook/export', { method: 'POST', body: JSON.stringify({ ids: ids?.length ? ids : null }) }),
}

export function asOutlookEmail(row: OutlookAccount): Pick<EmailRecord, 'sourceType' | 'outlookAccountId'> {
  return { sourceType: 'outlook', outlookAccountId: row.id }
}
