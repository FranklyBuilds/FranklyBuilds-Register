import type { ImportIssue, ImportPreview, ParsedEmail, ParsedProxy, ProxyScheme } from '@/types'

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const ICSMS_KEY_PATTERN = /^tok_[A-Za-z0-9_-]+$/

function linesOf(rawText: string) {
  return rawText.replace(/^\uFEFF/, '').split(/\r?\n/)
}

function validEmailAccessUrl(value: string, email: string) {
  try {
    const url = new URL(value)
    if (!['http:', 'https:'].includes(url.protocol)) return false
    if (!value.includes('#')) return true
    const fragmentIndex = value.indexOf('#')
    const rawBase = value.slice(0, fragmentIndex)
    const schemeEnd = rawBase.indexOf('://')
    const rawTarget = schemeEnd >= 0 ? rawBase.slice(schemeEnd + 3) : ''
    const pathStart = rawTarget.indexOf('/')
    const rawScheme = schemeEnd >= 0 ? rawBase.slice(0, schemeEnd).toLowerCase() : ''
    const rawAuthority = pathStart >= 0 ? rawTarget.slice(0, pathStart).toLowerCase() : ''
    const rawPath = pathStart >= 0 ? rawTarget.slice(pathStart) : ''
    const rawFragment = value.slice(fragmentIndex + 1)
    const fragmentFields = rawFragment.split('&')
    const parameters = new URLSearchParams(rawFragment)
    const entries = Array.from(parameters.entries())
    const sourceEmails = parameters.getAll('email')
    const keys = parameters.getAll('key')
    const sourceEmail = sourceEmails.length === 1
      ? (sourceEmails.at(0)?.trim().toLowerCase() ?? '')
      : ''
    const key = keys.length === 1 ? (keys.at(0)?.trim() ?? '') : ''
    return (
      url.protocol === 'https:' &&
      rawScheme === 'https' &&
      rawAuthority === 'icsms.top' &&
      rawPath === '/pickup' &&
      url.hostname.toLowerCase() === 'icsms.top' &&
      url.host.toLowerCase() === 'icsms.top' &&
      !url.username &&
      !url.password &&
      url.pathname === '/pickup' &&
      fragmentFields.length === 2 &&
      fragmentFields.every(Boolean) &&
      entries.length === 2 &&
      new Set(entries.map(([name]) => name)).size === 2 &&
      sourceEmail.length <= 320 &&
      EMAIL_PATTERN.test(sourceEmail) &&
      sourceEmail === email.trim().toLowerCase() &&
      key.length <= 512 &&
      ICSMS_KEY_PATTERN.test(key)
    )
  } catch {
    return false
  }
}

function maskEmail(value: string) {
  const [name = '', domain] = value.split('@')
  if (!domain) return '格式不可识别'
  const visible = name.slice(0, Math.min(2, name.length))
  return `${visible}${'•'.repeat(Math.max(2, Math.min(8, name.length - visible.length)))}@${domain}`
}

function redactLine(value: string) {
  const trimmed = value.trim()
  if (!trimmed) return '空行'
  const emailSeparator = trimmed.indexOf('----')
  if (emailSeparator >= 0) {
    const emailCandidate = trimmed.slice(0, emailSeparator).trim()
    return emailCandidate.includes('@')
      ? maskEmail(emailCandidate)
      : `${emailCandidate.slice(0, 8) || '无邮箱'}----••••`
  }
  const proxyParts = trimmed.split(':')
  if (/^(?:https?|socks(?:5h?)?):\/\//i.test(trimmed)) {
    try {
      const url = new URL(trimmed)
      return `${url.protocol}//ACCOUNT:TOKEN@${url.hostname}:${url.port}`
    } catch {
      return '代理 URL 格式不可识别'
    }
  }
  const authSeparator = trimmed.lastIndexOf('@')
  if (authSeparator > 0) return `${trimmed.slice(authSeparator + 1)}:••••:••••`
  if (proxyParts.length >= 2) return `${proxyParts[0]}:${proxyParts[1]}:••••:••••`
  return `${trimmed.slice(0, 14)}${trimmed.length > 14 ? '…' : ''}`
}

function decodeProxyCredential(value: string) {
  if (/%(?![0-9a-f]{2})/i.test(value)) return null
  try {
    return decodeURIComponent(value).trim()
  } catch {
    return null
  }
}

function parseCsvFields(value: string): string[] | null {
  const fields: string[] = []
  let current = ''
  let quoted = false
  let quoteClosed = false

  for (let index = 0; index < value.length; index += 1) {
    const character = value[index] ?? ''
    if (quoted) {
      if (character === '"') {
        if (value[index + 1] === '"') {
          current += '"'
          index += 1
        } else {
          quoted = false
          quoteClosed = true
        }
      } else {
        current += character
      }
      continue
    }

    if (character === '"') {
      if (current.trim()) return null
      quoted = true
      continue
    }
    if (character === ',') {
      fields.push(current.trim())
      current = ''
      quoteClosed = false
      continue
    }
    if (quoteClosed && !/\s/.test(character)) return null
    current += character
  }

  if (quoted) return null
  fields.push(current.trim())
  return fields
}

function isValidIpv6(value: string) {
  const zoneIndex = value.indexOf('%')
  const address = zoneIndex >= 0 ? value.slice(0, zoneIndex) : value
  if (!address || (zoneIndex >= 0 && !/^[A-Za-z0-9_.-]+$/.test(value.slice(zoneIndex + 1)))) {
    return false
  }
  try {
    // URL implements the full IPv6 grammar, including compressed and
    // IPv4-mapped forms, without adding a runtime dependency.
    new URL(`http://[${address}]:1`)
    return true
  } catch {
    return false
  }
}

function parseProxyValue(value: string): { parsed?: ParsedProxy; reason?: string } {
  let text = value.trim()
  if (!text) return { reason: '代理内容为空' }

  // The package accepts socks:// as a shorthand for SOCKS5.
  if (/^socks:\/\//i.test(text)) text = `socks5://${text.slice(text.indexOf('://') + 3)}`

  if (text.includes('://')) {
    try {
      const schemeText = text.slice(0, text.indexOf('://')).toLowerCase()
      if (!['http', 'https', 'socks5', 'socks5h'].includes(schemeText)) {
        return { reason: '代理协议必须是 HTTP、HTTPS、SOCKS5 或 SOCKS5H' }
      }
      const url = new URL(text)
      const decodedUsername = decodeProxyCredential(url.username)
      const decodedPassword = decodeProxyCredential(url.password)
      if (decodedUsername === null || decodedPassword === null) {
        return { reason: '代理认证信息 URL 编码无效' }
      }
      const portText = explicitProxyPort(text)
      if (portText === null) return { reason: '代理必须包含端口' }
      const rawHost = explicitProxyHost(text)
      if (rawHost === null) return { reason: '代理 URL 格式无效' }
      if (url.pathname !== '' && url.pathname !== '/') {
        return { reason: '代理 URL 不得包含路径' }
      }
      if (url.search || url.hash || text.includes('?') || text.includes('#')) {
        return { reason: '代理 URL 不得包含查询参数或片段' }
      }
      return buildProxyValue(
        rawHost,
        String(portText),
        decodedUsername,
        decodedPassword,
        schemeText as ProxyScheme,
      )
    } catch {
      return { reason: '代理 URL 格式无效' }
    }
  }

  // Common CSV exports: host,port or host,port,user,password.  Check this
  // after URL syntax so commas in a URL-legal credential are not mistaken for
  // CSV delimiters (the backend parser follows the same order).
  if (text.includes(',')) {
    const fields = parseCsvFields(text)
    if (!fields) return { reason: 'CSV 代理包含未闭合或非法引号' }
    if (fields.length !== 2 && fields.length !== 4) {
      return { reason: 'CSV 代理必须包含 2 或 4 个字段' }
    }
    return buildProxyValue(
      fields[0] || '',
      fields[1] || '',
      fields[2] || '',
      fields.slice(3).join(',').trim(),
      'http',
    )
  }

  if (text.includes('@')) {
    const atIndex = text.lastIndexOf('@')
    const auth = text.slice(0, atIndex)
    text = `http://${text.slice(atIndex + 1)}`
    const separator = auth.indexOf(':')
    if (separator < 1) return { reason: '代理认证格式必须是 username:password' }
    const decodedUsername = decodeProxyCredential(auth.slice(0, separator))
    const decodedPassword = decodeProxyCredential(auth.slice(separator + 1))
    if (decodedUsername === null || decodedPassword === null) {
      return { reason: '代理认证信息 URL 编码无效' }
    }
    try {
      const url = new URL(text)
      const portText = explicitProxyPort(text)
      if (portText === null) return { reason: '代理必须包含端口' }
      const rawHost = explicitProxyHost(text)
      if (rawHost === null) return { reason: '代理 URL 格式无效' }
      if (url.pathname !== '' && url.pathname !== '/') {
        return { reason: '代理 URL 不得包含路径' }
      }
      if (url.search || url.hash || text.includes('?') || text.includes('#')) {
        return { reason: '代理 URL 不得包含查询参数或片段' }
      }
      return buildProxyValue(
        rawHost,
        String(portText),
        decodedUsername,
        decodedPassword,
        'http',
      )
    } catch {
      return { reason: '代理 URL 格式无效' }
    }
  }

  if (text.startsWith('[')) {
    const match = /^\[([^\]]+)\]:(\d+)(?::([^:]*):(.*))?$/.exec(text)
    if (!match) return { reason: 'IPv6 代理必须使用 [host]:port 格式' }
    if (!(match[1] || '').includes(':')) return { reason: 'IPv6 代理主机格式无效' }
    return buildProxyValue(match[1] || '', match[2] || '', match[3] || '', match[4] || '', 'http')
  }

  const parts = text.split(':')
  if (parts.length === 2) return buildProxyValue(parts[0] || '', parts[1] || '', '', '', 'http')
  if (parts.length >= 4) {
    return buildProxyValue(
      parts[0] || '',
      parts[1] || '',
      parts[2] || '',
      parts.slice(3).join(':').trim(),
      'http',
    )
  }
  return { reason: '代理必须是 URL、host:port 或 host:port:user:pass' }
}

function explicitProxyHost(value: string): string | null {
  const schemeEnd = value.indexOf('://')
  if (schemeEnd < 0) return null
  const authority = value.slice(schemeEnd + 3).split(/[/?#]/, 1)[0] || ''
  const hostPort = authority.slice(authority.lastIndexOf('@') + 1)
  if (hostPort.startsWith('[')) {
    const closeBracket = hostPort.indexOf(']')
    if (closeBracket < 0 || hostPort[closeBracket + 1] !== ':') return null
    return hostPort.slice(1, closeBracket)
  }
  const separator = hostPort.lastIndexOf(':')
  if (separator < 1) return null
  return hostPort.slice(0, separator)
}

function explicitProxyPort(value: string): string | null {
  const authority = value.slice(value.indexOf('://') + 3).split(/[/?#]/, 1)[0] || ''
  const hostPort = authority.slice(authority.lastIndexOf('@') + 1)
  if (hostPort.startsWith('[')) {
    const closeBracket = hostPort.indexOf(']')
    if (closeBracket < 0 || hostPort[closeBracket + 1] !== ':') return null
    const port = hostPort.slice(closeBracket + 2)
    return /^[0-9]+$/.test(port) ? port : null
  }
  const separator = hostPort.lastIndexOf(':')
  if (separator < 1) return null
  const port = hostPort.slice(separator + 1)
  return /^[0-9]+$/.test(port) ? port : null
}

function buildProxyValue(
  rawHost: string,
  rawPort: string,
  rawUsername: string,
  rawPassword: string,
  scheme: ProxyScheme,
): { parsed?: ParsedProxy; reason?: string } {
  let host = String(rawHost || '').trim()
  const startsBracket = host.startsWith('[')
  const endsBracket = host.endsWith(']')
  if (startsBracket || endsBracket) {
    if (!startsBracket || !endsBracket) return { reason: '代理主机格式无效' }
    host = host.slice(1, -1)
  }
  host = host.toLowerCase()
  const portText = String(rawPort || '').trim()
  if (!/^[0-9]+$/.test(portText)) {
    return { reason: '端口必须是 1–65535 的整数' }
  }
  const port = Number(portText)
  if (!host) return { reason: '代理主机不能为空' }
  if (/[\s/?#@\[\]\\^|<>]/.test(host)) return { reason: '代理主机格式无效' }
  if (host.includes(':')) {
    if (!isValidIpv6(host)) {
      return { reason: 'IPv6 代理主机格式无效' }
    }
  } else if (host.includes('%')) {
    return { reason: '代理主机格式无效' }
  }
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    return { reason: '端口必须是 1–65535 的整数' }
  }
  return {
    parsed: {
      host,
      port,
      username: String(rawUsername || '').trim(),
      password: String(rawPassword || '').trim(),
      scheme,
    },
  }
}

function proxyInputTokens(lineText: string): string[] {
  // A comma marks one CSV record.  Keep it intact so spaces in a quoted
  // username/password are not mistaken for separate proxy nodes.
  if (lineText.includes(',')) return [lineText]
  return lineText.split(/\s+/).filter(Boolean)
}

export function emailKey(email: string) {
  return email.trim().toLowerCase()
}

export function proxyKey(proxy: ParsedProxy) {
  // Match Mongo's existing unique index; scheme is mutable metadata on a
  // single endpoint rather than part of the storage identity.
  return `${proxy.host.toLowerCase()}:${proxy.port}:${proxy.username}:${proxy.password}`
}

export function parseEmailImport(
  rawText: string,
  existingEmails: Iterable<string> = [],
): ImportPreview<ParsedEmail> {
  const accepted: ParsedEmail[] = []
  const duplicates: ImportIssue[] = []
  const errors: ImportIssue[] = []
  const seen = new Set(Array.from(existingEmails, emailKey))
  let total = 0

  linesOf(rawText).forEach((source, index) => {
    const value = source.trim()
    if (!value) return
    total += 1
    const line = index + 1
    const separatorIndex = value.indexOf('----')

    if (separatorIndex < 1) {
      errors.push({ line, reason: '缺少 ---- 分隔符', preview: redactLine(value) })
      return
    }

    const email = value.slice(0, separatorIndex).trim()
    const credential = value.slice(separatorIndex + 4).trim()

    if (!EMAIL_PATTERN.test(email)) {
      errors.push({ line, reason: '邮箱格式无效', preview: redactLine(value) })
      return
    }

    const isHttpUrl = validEmailAccessUrl(credential, email)
    const isMailcomPassword = credential.length > 0 && credential.length <= 1024 && !credential.includes('://')
    if (!isHttpUrl && !isMailcomPassword) {
      errors.push({ line, reason: '第二段必须是接码 URL 或 mail.com 密码', preview: maskEmail(email) })
      return
    }

    const key = emailKey(email)
    if (seen.has(key)) {
      duplicates.push({ line, reason: '邮箱已存在或在本批次重复', preview: maskEmail(email) })
      return
    }

    seen.add(key)
    accepted.push({ email, accessUrl: credential })
  })

  return { total, accepted, duplicates, errors }
}

export function parseProxyImport(
  rawText: string,
  existingKeys: Iterable<string> = [],
): ImportPreview<ParsedProxy> {
  const accepted: ParsedProxy[] = []
  const duplicates: ImportIssue[] = []
  const errors: ImportIssue[] = []
  const seen = new Set(existingKeys)
  let total = 0

  const lines = rawText.replace(/^\uFEFF/, '').split(/\r?\n/)
  lines.forEach((source, lineIndex) => {
    const lineText = source.trim()
    if (!lineText || lineText.startsWith('#') || lineText.startsWith('//')) return
    const values = proxyInputTokens(lineText)
    values.forEach((value) => {
      if (!value) return
      total += 1
      const line = lineIndex + 1
      const result = parseProxyValue(value)
      if (!result.parsed) {
        errors.push({ line, reason: result.reason || '代理格式无效', preview: redactLine(value) })
        return
      }
      const key = proxyKey(result.parsed)
      if (seen.has(key)) {
        duplicates.push({ line, reason: '代理已存在或在本批次重复', preview: redactLine(value) })
        return
      }

      seen.add(key)
      accepted.push(result.parsed)
    })
  })

  return { total, accepted, duplicates, errors }
}
