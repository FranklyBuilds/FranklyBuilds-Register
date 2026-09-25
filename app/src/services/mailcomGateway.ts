export interface MailComAccount {
  id: string
  email: string
  status: string
  messageCount: number | null
  lastCheckedAt: string | null
  lastError: string | null
  createdAt: string | null
  updatedAt: string | null
  aliasCount: number
}
export interface MailComAlias { id: string; accountId: string; email: string; label: string; createdAt: string | null; updatedAt: string | null }
export interface MailComMessage { id?: string; subject: string; sender: string; recipients: string; receivedAt: string | null; folder: string; preview: string; verificationCode: string | null }
export interface MailComPage { items: MailComAccount[]; total: number; page: number; pageSize: number }
export interface MailComMigrationResult { status: string; source?: string; backup?: string; accounts?: number; aliases?: number; imported?: number; duplicates?: number; errors?: number; completedAt?: string }

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...init, headers: { ...(init.body ? { 'Content-Type': 'application/json' } : {}), ...(init.headers || {}) } })
  const raw = await response.text()
  let body: any = null
  try { body = raw ? JSON.parse(raw) : null } catch { body = raw }
  if (!response.ok) {
    const detail = body?.detail || body?.error || response.statusText
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return body as T
}

export const mailcomGateway = {
  health: () => request<{ status: string; service: string; storage: string; legacyPort: number | null }>('/api/mailcom/health'),
  accounts: (page = 1, pageSize = 50, q = '') => request<MailComPage>(`/api/mailcom/accounts?${new URLSearchParams({ page: String(page), pageSize: String(pageSize), q })}`),
  importAccounts: (rawText: string) => request<{ total: number; imported: number; duplicateCount: number; errorCount: number }>('/api/mailcom/accounts/import', { method: 'POST', body: JSON.stringify({ rawText }) }),
  deleteAccount: (id: string) => request<{ deleted: boolean }>(`/api/mailcom/accounts/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  aliases: (id: string) => request<{ items: MailComAlias[] }>(`/api/mailcom/accounts/${encodeURIComponent(id)}/aliases`),
  importAlias: (id: string, email: string, label = '') => request<{ status: string }>(`/api/mailcom/accounts/${encodeURIComponent(id)}/aliases/import`, { method: 'POST', body: JSON.stringify({ email, label }) }),
  deleteAlias: (id: string) => request<{ deleted: boolean }>(`/api/mailcom/aliases/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  testAccount: (id: string) => request<{ id: string; email: string; ok: boolean; messageCount?: number; error?: { code: string; message: string } }>(`/api/mailcom/accounts/${encodeURIComponent(id)}/test`, { method: 'POST' }),
  messages: (id: string, folder = 'INBOX', limit = 30) => request<{ accountId: string; email: string; folder: string; items: MailComMessage[] }>(`/api/mailcom/accounts/${encodeURIComponent(id)}/messages?${new URLSearchParams({ folder, limit: String(limit) })}`),
  latestCode: (id: string) => request<{ found: boolean; email: string; message: MailComMessage | null }>(`/api/mailcom/accounts/${encodeURIComponent(id)}/latest-code`),
  migrate: () => request<MailComMigrationResult>('/api/mailcom/migrate', { method: 'POST' }),
  serverSync: (input: { host: string; port: number; username: string; password: string }) => request<{ ok: boolean; accounts: number; aliases: number; hostKeySha256: string }>('/api/mailcom/server-sync', { method: 'POST', body: JSON.stringify(input) }),
}
