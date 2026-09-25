<script setup lang='ts'>
import { computed } from 'vue'
import type { OutlookOAuthConfig } from '@/services/outlookGateway'
const props = defineProps<{ modelValue: OutlookOAuthConfig }>()
const emit = defineEmits<{ 'update:modelValue': [value: OutlookOAuthConfig] }>()
function field<K extends keyof OutlookOAuthConfig>(key: K) {
  return computed({
    get: () => props.modelValue[key],
    set: (value: OutlookOAuthConfig[K]) => emit('update:modelValue', { ...props.modelValue, [key]: value }),
  })
}
const enabled = field('enable_oauth2')
const clientId = field('client_id')
const redirectUrl = field('redirect_url')
const scopes = field('scopes')
const scopeText = computed({
  get: () => Array.isArray(scopes.value) ? scopes.value.join('\n') : scopes.value,
  set: (value: string) => { scopes.value = value },
})
</script>
<template>
  <el-form-item label='OAuth 校验'><el-switch v-model='enabled' aria-label='OAuth 校验' /></el-form-item>
  <el-form-item label='Client ID'>
    <div>
      <el-input v-model='clientId' type='password' show-password autocomplete='new-password' aria-label='OAuth Client ID' placeholder='留空保留已保存值' />
      <small>{{ modelValue.clientIdConfigured ? '已配置；普通接口不回显' : '尚未配置 Client ID' }}</small>
    </div>
  </el-form-item>
  <el-form-item label='回调地址'><el-input v-model='redirectUrl' aria-label='OAuth 回调地址' placeholder='https://localhost' /></el-form-item>
  <el-form-item label='权限范围'><el-input v-model='scopeText' type='textarea' :rows='3' aria-label='OAuth 权限范围' placeholder='每行一个 scope；与 OAuth 应用授权配置保持一致' /></el-form-item>
</template>
