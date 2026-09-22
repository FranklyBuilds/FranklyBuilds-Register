import { describe, expect, it } from 'vitest'
import { parseEmailImport, parseProxyImport, proxyKey } from './parsers'

describe('parseEmailImport', () => {
  it('cleans BOM and blank lines and accepts the documented format', () => {
    const result = parseEmailImport(
      '\uFEFFdemo@example.com----https://example.com/inbox/token\n\n  second@example.com----https://example.com/s/2  ',
    )

    expect(result.total).toBe(2)
    expect(result.accepted).toEqual([
      { email: 'demo@example.com', accessUrl: 'https://example.com/inbox/token' },
      { email: 'second@example.com', accessUrl: 'https://example.com/s/2' },
    ])
  })

  it('skips existing and in-batch duplicate emails case-insensitively', () => {
    const result = parseEmailImport(
      'DUP@example.com----https://example.com/1\nnew@example.com----https://example.com/2\nNEW@example.com----https://example.com/3',
      ['dup@example.com'],
    )

    expect(result.accepted).toHaveLength(1)
    expect(result.duplicates).toHaveLength(2)
  })

  it('accepts mail.com branded mailboxes with an IMAP password', () => {
    const result = parseEmailImport(
      'person@gardener.com----mail-password-1\nsecond@fireman.net----mail-password-2',
    )

    expect(result.errors).toHaveLength(0)
    expect(result.accepted).toEqual([
      { email: 'person@gardener.com', accessUrl: 'mail-password-1' },
      { email: 'second@fireman.net', accessUrl: 'mail-password-2' },
    ])
  })

  it('rejects invalid email and unsupported URL without exposing the URL token', () => {
    const result = parseEmailImport(
      'invalid----https://example.com/s/super-secret-token\nok@example.com----file:///private/token',
    )

    expect(result.errors).toHaveLength(2)
    expect(JSON.stringify(result.errors)).not.toContain('super-secret-token')
    expect(JSON.stringify(result.errors)).not.toContain('/private/token')
  })

  it('accepts only valid icsms pickup fragments', () => {
    const pickupUrl = 'https://icsms.top/pickup#email=person%40example.com&key=tok_TEST_fixture_123'
    const result = parseEmailImport(
      [
        `person@example.com----${pickupUrl}`,
        'generic@example.com----https://mail.example/inbox#token=secret',
        'other@example.com----https://icsms.top/pickup#email=mismatch%40example.com&key=tok_TEST_fixture_456',
        'broken@example.com----https://icsms.top/pickup#email=broken%40example.com&key=invalid-token',
        'trailing@example.com----https://icsms.top/pickup#email=trailing%40example.com&key=tok_TEST_fixture_789&',
        'port@example.com----https://icsms.top:443/pickup#email=port%40example.com&key=tok_TEST_fixture_012',
        'slashes@example.com----https:////icsms.top/pickup#email=slashes%40example.com&key=tok_TEST_fixture_345',
      ].join('\n'),
    )

    expect(result.total).toBe(7)
    expect(result.accepted).toEqual([
      { email: 'person@example.com', accessUrl: pickupUrl },
    ])
    expect(result.errors).toHaveLength(6)
    expect(JSON.stringify(result.errors)).not.toContain('secret')
    expect(JSON.stringify(result.errors)).not.toContain('tok_TEST_fixture_456')
  })
})

describe('parseProxyImport', () => {
  it('accepts unauthenticated HTTP/SOCKS URLs and host:port entries', () => {
    const result = parseProxyImport(
      'http://proxy.example.com:8080\nsocks5://[::1]:1080\nproxy.example.com:3128',
    )

    expect(result.errors).toHaveLength(0)
    expect(result.accepted).toEqual([
      { host: 'proxy.example.com', port: 8080, username: '', password: '', scheme: 'http' },
      { host: '::1', port: 1080, username: '', password: '', scheme: 'socks5' },
      { host: 'proxy.example.com', port: 3128, username: '', password: '', scheme: 'http' },
    ])
  })

  it('normalizes the socks:// alias and keeps expanded IPv6 text stable', () => {
    const result = parseProxyImport(
      'socks://proxy.example.com:1080\nhttp://[2001:0db8:0:0::1]:8080',
    )

    expect(result.errors).toHaveLength(0)
    expect(result.accepted).toEqual([
      { host: 'proxy.example.com', port: 1080, username: '', password: '', scheme: 'socks5' },
      { host: '2001:0db8:0:0::1', port: 8080, username: '', password: '', scheme: 'http' },
    ])
  })

  it('accepts userinfo and CSV proxy forms without exposing credentials in errors', () => {
    const result = parseProxyImport(
      'user:pass@proxy.example.com:10000\nproxy.example.com,10001,csv-user,csv-pass\n# ignored comment',
    )

    expect(result.errors).toHaveLength(0)
    expect(result.accepted).toEqual([
      { host: 'proxy.example.com', port: 10000, username: 'user', password: 'pass', scheme: 'http' },
      { host: 'proxy.example.com', port: 10001, username: 'csv-user', password: 'csv-pass', scheme: 'http' },
    ])

    const malformed = parseProxyImport('user:secret@proxy.example.com:not-a-port')
    expect(JSON.stringify(malformed.errors)).not.toContain('secret')
  })

  it('matches backend CSV quoting and rejects non-decimal ports and URL suffixes', () => {
    const quoted = parseProxyImport('proxy.example.com,10001,csv-user,"p, q"')
    expect(quoted.errors).toHaveLength(0)
    expect(quoted.accepted[0]).toMatchObject({
      host: 'proxy.example.com',
      port: 10001,
      username: 'csv-user',
      password: 'p, q',
    })

    expect(parseProxyImport('proxy.example.com:1e3').errors).toHaveLength(1)
    expect(parseProxyImport('http://proxy.example.com:8080/path').errors).toHaveLength(1)
    expect(parseProxyImport('socks://proxy.example.com:8080?source=pool').errors).toHaveLength(1)
    expect(parseProxyImport('http://proxy.example.com:8080#fragment').errors).toHaveLength(1)
    expect(parseProxyImport('http://proxy.example.com:8080?').errors).toHaveLength(1)
    expect(parseProxyImport('http://proxy.example.com:8080#').errors).toHaveLength(1)
    expect(parseProxyImport('http://[1:2:3]:8080').errors).toHaveLength(1)
    expect(parseProxyImport('[proxy.example.com]:8080').errors).toHaveLength(1)
    expect(parseProxyImport('[fe80::1%bad!]:8080').errors).toHaveLength(1)
    expect(parseProxyImport('proxy.example.com,10001,user,"bad"tail').errors).toHaveLength(1)
    expect(parseProxyImport('http://user:bad%2@proxy.example.com:8080').errors).toHaveLength(1)
  })

  it('accepts a password containing colons', () => {
    const result = parseProxyImport('proxy.example.com:10000:user:pass:with:colons')
    expect(result.accepted).toEqual([
      { host: 'proxy.example.com', port: 10000, username: 'user', password: 'pass:with:colons', scheme: 'http' },
    ])
  })

  it('keeps URL commas in credentials separate from CSV detection', () => {
    const result = parseProxyImport('http://user:p,q@proxy.example.com:10000')
    expect(result.errors).toHaveLength(0)
    expect(result.accepted).toEqual([
      { host: 'proxy.example.com', port: 10000, username: 'user', password: 'p,q', scheme: 'http' },
    ])
  })

  it('accepts whitespace-separated SOCKS5 URLs and preserves the scheme', () => {
    const result = parseProxyImport(
      'socks5://user-region-GB-one:pass@proxy.example.com:3000 socks5://user-region-GB-two:pass@proxy.example.com:3000',
    )
    expect(result.total).toBe(2)
    expect(result.errors).toHaveLength(0)
    expect(result.accepted.map((item) => item.scheme)).toEqual(['socks5', 'socks5'])
  })

  it.each(['host:0:user:pass', 'host:65536:user:pass', 'host:nope:user:pass'])(
    'rejects invalid port in %s',
    (input) => {
      expect(parseProxyImport(input).errors).toHaveLength(1)
    },
  )

  it('skips exact existing proxy records', () => {
    const parsed = { host: 'proxy.example.com', port: 10000, username: 'user', password: 'pass', scheme: 'http' as const }
    const result = parseProxyImport(
      'proxy.example.com:10000:user:pass',
      [proxyKey(parsed)],
    )
    expect(result.accepted).toHaveLength(0)
    expect(result.duplicates).toHaveLength(1)
  })

  it('uses the endpoint identity shared by Mongo when protocols differ', () => {
    const existing = {
      host: 'proxy.example.com',
      port: 10000,
      username: 'user',
      password: 'pass',
      scheme: 'http' as const,
    }
    const result = parseProxyImport('socks5://user:pass@proxy.example.com:10000', [proxyKey(existing)])
    expect(result.accepted).toHaveLength(0)
    expect(result.duplicates).toHaveLength(1)
  })
})
