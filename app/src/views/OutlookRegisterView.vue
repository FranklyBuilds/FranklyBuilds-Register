<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { Connection, Refresh, VideoPlay, VideoPause, Delete } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { getOutlookBaseUrl, outlookGateway, setOutlookConnection, type OutlookRegisterState } from '@/services/outlookGateway'

const apiBase = ref(getOutlookBaseUrl())
const adminToken = ref('')
const connected = ref(false)
const loading = ref(false)
const saving = ref(false)
const state = ref<OutlookRegisterState | null>(null)
const scopesText = ref('')
let timer: ReturnType<typeof setInterval> | undefined

const form = reactive({
  email_suffix: '@outlook.com', headless: false, captcha_strategy: 0,
  concurrent_flows: 1, tasks: 1, success_tasks: null as number | null,
  batch_success_limit: 300, bot_protection_wait: 15, max_captcha_retries: 3,
  proxy: { mode: 'single' as 'single' | 'multiple', type: 'http' as 'http' | 'https' | 'socks5' | 'socks5h', host: '', single_port: 0, port_start: 0, port_end: 0, max_per_proxy: 20 },
  oauth2: { enable_oauth2: true, client_id: '', redirect_url: 'https://localhost', Scopes: [] as string[] },
  temp_mail: { enabled: false, base_url: '', admin_password: '', domain: '', name_prefix: '', enable_prefix: false, code_timeout: 120, poll_interval: 3 },
})

const running = computed(() => Boolean(
  state.value?.running
  || state.value?.enabled
  || state.value?.status === 'running'
  || state.value?.stats?.status === 'running',
))
const statsText = computed(() => {
  const stats = state.value?.stats || {}
  return `提交 ${stats.submitted ?? 0} · 成功 ${stats.succeeded ?? 0} · 失败 ${stats.failed ?? 0}`
})

function applyState(next: OutlookRegisterState) {
  state.value = next
  const source: any = next.config || next
  for (const key of ['email_suffix', 'headless', 'captcha_strategy', 'concurrent_flows', 'tasks', 'success_tasks', 'batch_success_limit', 'bot_protection_wait', 'max_captcha_retries']) {
    if (source[key] !== undefined) (form as any)[key] = source[key]
  }
  if (source.proxy) Object.assign(form.proxy, source.proxy)
  if (source.oauth2) {
    Object.assign(form.oauth2, source.oauth2)
    scopesText.value = Array.isArray(source.oauth2.Scopes) ? source.oauth2.Scopes.join('\n') : ''
  }
  if (source.temp_mail) Object.assign(form.temp_mail, source.temp_mail)
}

async function connect() {
  loading.value = true
  try {
    setOutlookConnection(apiBase.value, adminToken.value)
    await outlookGateway.health()
    applyState(await outlookGateway.getRegister())
    connected.value = true
    ElMessage.success('Outlook 服务已连接')
  } catch (error) {
    connected.value = false
    ElMessage.error(error instanceof Error ? error.message : 'Outlook 服务连接失败')
  } finally { loading.value = false }
}

async function refresh() {
  if (!connected.value) return
  try { applyState(await outlookGateway.getRegister()) } catch (error) { ElMessage.error(error instanceof Error ? error.message : '读取 Outlook 状态失败') }
}

async function save() {
  saving.value = true
  try {
    const payload: any = JSON.parse(JSON.stringify(form))
    payload.oauth2.Scopes = scopesText.value.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean)
    applyState(await outlookGateway.saveRegister(payload))
    ElMessage.success('Outlook 注册配置已保存')
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '保存失败') }
  finally { saving.value = false }
}

async function action(name: 'start' | 'stop' | 'reset') {
  try {
    const result = await outlookGateway[name]()
    applyState(result)
    ElMessage.success(name === 'start' ? '注册任务已启动' : name === 'stop' ? '注册任务已停止' : '任务状态已重置')
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '操作失败') }
}

onMounted(() => { timer = setInterval(() => void refresh(), 2500) })
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="outlook-view">
    <el-card shadow="never" class="connection-card">
      <template #header><div class="card-header"><span>Outlook 注册服务</span><el-tag :type="connected ? 'success' : 'info'">{{ connected ? '已连接' : '未连接' }}</el-tag></div></template>
      <el-form :inline="true" @submit.prevent="connect">
        <el-form-item label="API 地址"><el-input v-model="apiBase" placeholder="http://127.0.0.1:8001" /></el-form-item>
        <el-form-item label="管理令牌"><el-input v-model="adminToken" type="password" show-password placeholder="可选" /></el-form-item>
        <el-form-item><el-button type="primary" :loading="loading" @click="connect"><el-icon><Connection /></el-icon>连接</el-button><el-button :disabled="!connected" @click="refresh"><el-icon><Refresh /></el-icon>刷新</el-button></el-form-item>
      </el-form>
    </el-card>

    <el-card v-if="connected" shadow="never" class="config-card">
      <template #header><div class="card-header"><span>Outlook 任务与代理</span><div><el-button type="primary" :loading="saving" @click="save">保存配置</el-button><el-button :disabled="running" @click="action('start')"><el-icon><VideoPlay /></el-icon>启动</el-button><el-button :disabled="!running" @click="action('stop')"><el-icon><VideoPause /></el-icon>停止</el-button><el-button @click="action('reset')"><el-icon><Delete /></el-icon>重置</el-button></div></div></template>
      <el-form label-position="top" class="outlook-form">
        <el-divider content-position="left">任务</el-divider>
        <el-row :gutter="16">
          <el-col :span="6"><el-form-item label="邮箱后缀"><el-select v-model="form.email_suffix"><el-option label="@outlook.com" value="@outlook.com" /><el-option label="@hotmail.com" value="@hotmail.com" /></el-select></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="验证码策略"><el-select v-model="form.captcha_strategy"><el-option label="全自动" :value="0" /><el-option label="半自动" :value="1" /></el-select></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="并发数"><el-input-number v-model="form.concurrent_flows" :min="1" :max="50" /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="任务数"><el-input-number v-model="form.tasks" :min="1" /></el-form-item></el-col>
        </el-row>
        <el-divider content-position="left">代理</el-divider>
        <el-row :gutter="16">
          <el-col :span="5"><el-form-item label="模式"><el-select v-model="form.proxy.mode"><el-option label="单端口" value="single" /><el-option label="端口池" value="multiple" /></el-select></el-form-item></el-col>
          <el-col :span="5"><el-form-item label="协议"><el-select v-model="form.proxy.type"><el-option v-for="type in ['http','https','socks5','socks5h']" :key="type" :label="type" :value="type" /></el-select></el-form-item></el-col>
          <el-col :span="7"><el-form-item label="代理主机"><el-input v-model="form.proxy.host" placeholder="127.0.0.1 或 host.docker.internal" /></el-form-item></el-col>
          <el-col v-if="form.proxy.mode === 'single'" :span="7"><el-form-item label="端口"><el-input-number v-model="form.proxy.single_port" :min="0" :max="65535" /></el-form-item></el-col>
          <template v-else><el-col :span="3"><el-form-item label="起始端口"><el-input-number v-model="form.proxy.port_start" :min="0" :max="65535" /></el-form-item></el-col><el-col :span="4"><el-form-item label="结束端口"><el-input-number v-model="form.proxy.port_end" :min="0" :max="65535" /></el-form-item></el-col></template>
        </el-row>
        <el-row :gutter="16"><el-col :span="6"><el-form-item label="每端口最大使用次数"><el-input-number v-model="form.proxy.max_per_proxy" :min="1" /></el-form-item></el-col><el-col :span="6"><el-form-item label="无头模式"><el-switch v-model="form.headless" /></el-form-item></el-col><el-col :span="8"><el-form-item label="状态"><el-tag :type="running ? 'warning' : 'info'">{{ state?.status || state?.stats?.status || 'idle' }} · {{ statsText }}</el-tag></el-form-item></el-col></el-row>
        <el-divider content-position="left">OAuth2</el-divider>
        <el-row :gutter="16">
          <el-col :span="6"><el-form-item label="获取 refresh_token"><el-switch v-model="form.oauth2.enable_oauth2" /></el-form-item></el-col>
          <el-col :span="9"><el-form-item label="client_id"><el-input v-model="form.oauth2.client_id" /></el-form-item></el-col>
          <el-col :span="9"><el-form-item label="redirect_url"><el-input v-model="form.oauth2.redirect_url" /></el-form-item></el-col>
          <el-col :span="24"><el-form-item label="Scopes（空格或换行分隔）"><el-input v-model="scopesText" type="textarea" :rows="2" /></el-form-item></el-col>
        </el-row>
        <el-divider content-position="left">辅助邮箱绑定</el-divider>
        <el-row :gutter="16">
          <el-col :span="6"><el-form-item label="启用临时邮箱"><el-switch v-model="form.temp_mail.enabled" /></el-form-item></el-col>
          <el-col :span="9"><el-form-item label="接口地址"><el-input v-model="form.temp_mail.base_url" /></el-form-item></el-col>
          <el-col :span="9"><el-form-item label="管理员密码"><el-input v-model="form.temp_mail.admin_password" type="password" show-password /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="邮箱域名"><el-input v-model="form.temp_mail.domain" /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="邮箱名前缀"><el-input v-model="form.temp_mail.name_prefix" /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="使用前缀"><el-switch v-model="form.temp_mail.enable_prefix" /></el-form-item></el-col>
          <el-col :span="3"><el-form-item label="验证码超时"><el-input-number v-model="form.temp_mail.code_timeout" :min="10" /></el-form-item></el-col>
          <el-col :span="3"><el-form-item label="轮询间隔"><el-input-number v-model="form.temp_mail.poll_interval" :min="1" /></el-form-item></el-col>
        </el-row>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.outlook-view { display: grid; gap: 16px; }
.connection-card, .config-card { border-radius: 12px; }
.card-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.card-header > div { display: flex; gap: 8px; flex-wrap: wrap; }
.outlook-form :deep(.el-input-number), .outlook-form :deep(.el-select) { width: 100%; }
@media (max-width: 900px) { .card-header { align-items: flex-start; flex-direction: column; } }
</style>
