import { effectScope } from 'vue'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { useOutlookRegisterConfig } from './useOutlookRegisterConfig'

describe('Outlook configuration drafts', () => {
  let scope: ReturnType<typeof effectScope>
  let editor: ReturnType<typeof useOutlookRegisterConfig>
  beforeEach(() => { scope = effectScope(); editor = scope.run(useOutlookRegisterConfig)! })
  afterEach(() => scope.stop())

  it('preserves local OAuth edits when server status is polled', () => {
    editor.accept({ tasks: 1 })
    expect(editor.dirty.value).toBe(false)
    editor.config.value.oauth2.client_id = 'CLIENT_ID_FIXTURE'
    editor.config.value.proxy.group = 'draft-group'
    editor.accept({ tasks: 99 })
    expect(editor.config.value.tasks).toBe(1)
    expect(editor.config.value.oauth2.client_id).toBe('CLIENT_ID_FIXTURE')
    expect(editor.config.value.proxy.group).toBe('draft-group')
    expect(editor.dirty.value).toBe(true)
  })

  it('normalizes scopes and never submits response-only flags or blank credentials', () => {
    editor.config.value.oauth2.scopes = 'offline_access, Mail.Read\nUser.Read'
    editor.config.value.oauth2.clientIdConfigured = true
    editor.config.value.oauth2.client_id = '   '
    editor.config.value.temp_mail = { adminPasswordConfigured: true }
    const payload = editor.payload()
    expect(payload.oauth2.scopes).toEqual(['offline_access', 'Mail.Read', 'User.Read'])
    expect(payload.oauth2).not.toHaveProperty('clientIdConfigured')
    expect(payload.oauth2).not.toHaveProperty('client_id')
    expect(payload.temp_mail).not.toHaveProperty('adminPasswordConfigured')
    expect(editor.config.value.oauth2.clientIdConfigured).toBe(true)
  })

  it('clears the write-only input after successful save without mutating the response', () => {
    editor.config.value.oauth2.client_id = 'CLIENT_ID_FIXTURE'
    const response = { ...editor.payload(), oauth2: { ...editor.payload().oauth2, clientIdConfigured: true } }
    delete response.oauth2.client_id
    editor.accept(response, true)
    expect(editor.dirty.value).toBe(false)
    expect(editor.config.value.oauth2.client_id).toBe('')
    expect(response.oauth2).not.toHaveProperty('client_id')
  })

  it('supports deliberately clearing all scopes', () => {
    editor.config.value.oauth2.scopes = '   '
    expect(editor.payload().oauth2.scopes).toEqual([])
  })
})
