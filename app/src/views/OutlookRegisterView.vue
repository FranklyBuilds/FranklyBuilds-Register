<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Delete, Download, Edit, Message, Refresh, UploadFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { outlookGateway, type OutlookAccount, type OutlookMessage, type OutlookPoolCheckConfig, type OutlookPoolItem, type OutlookPoolStats, type OutlookProxy, type OutlookRegisterSnapshot } from '@/services/outlookGateway'
import type { ProxyGroupSummary } from '@/types'
import OutlookOAuthConfig from '@/components/OutlookOAuthConfig.vue'
import { useOutlookRegisterConfig } from '@/composables/useOutlookRegisterConfig'

const rows = ref<OutlookAccount[]>([])
const messages = ref<OutlookMessage[]>([])
const selected = ref<OutlookAccount | null>(null)
const proxies = ref<OutlookProxy[]>([])
const proxyGroups = ref<ProxyGroupSummary[]>([])
const registerStatus = ref<OutlookRegisterSnapshot | null>(null)
const loading = ref(false)
const checkingId = ref('')
const importing = ref(false)
const importText = ref('')
const filter = ref('all')
const search = ref('')
const migration = ref<Record<string, unknown> | null>(null)
const resultRows = ref<Array<{ email: string; oauthStatus: string; graphStatus: string; createdAt: string }>>([])
const proxyTotal = ref(0)
const registerLogs = ref<Array<{ createdAt: string; level: string; line: string }>>([])
const registerAction = ref(false)
const { config: registerConfig, dirty: registerConfigDirty, accept: acceptRegisterConfig, payload: registerConfigPayload, generation: registerConfigRevision } = useOutlookRegisterConfig()
const poolStats = ref<OutlookPoolStats | null>(null)
const poolRows = ref<OutlookPoolItem[]>([])
const poolCategory = ref('all')
const poolSearch = ref('')
const poolSelectedIds = ref<string[]>([])
const poolBusy = ref(false)
const poolCheckConfig = ref<OutlookPoolCheckConfig | null>(null)
const poolSubCount = ref(1)
const poolTagPrefix = ref('')
const poolOtp = ref<Record<string, string>>({})

const proxyGroupCount = computed(() => proxyGroups.value.length)
const registrationSummary = computed(() => {
  const stats = registerStatus.value?.stats || {}
  return `Outlook 独立任务：${registerStatus.value?.status || stats.status || 'idle'} · 提交 ${stats.submitted || 0} · 成功 ${stats.succeeded || 0} · 失败 ${stats.failed || 0}`
})

function statusType(value: string) {
  if (value === 'available' || value === 'ok') return 'success'
  if (value === 'assigned') return 'warning'
  if (value === 'expired' || value === 'error' || value === 'conflict' || value === 'unavailable') return 'danger'
  return 'info'
}

let taskTimer: number | undefined

async function refreshTaskState() {
  const configRevision = registerConfigRevision()
  try {
    const [status, logs] = await Promise.all([outlookGateway.registerStatus(), outlookGateway.registerLogs()])
    registerStatus.value = status
    acceptRegisterConfig(status.config, false, configRevision)
    registerLogs.value = logs.items
  } catch { /* the main refresh displays transport failures */ }
}

async function refresh() {
  const configRevision = registerConfigRevision()
  loading.value = true
  try {
    const [page, migrationResult, result, proxyPage, groups, status, poolStatsResult, poolPage, poolConfigResult] = await Promise.all([
      outlookGateway.list({ page: 1, pageSize: 200, q: search.value, poolStatus: filter.value }),
      outlookGateway.migration(),
      outlookGateway.results(100, search.value),
      outlookGateway.proxies(1, 100),
      outlookGateway.proxyGroups(),
      outlookGateway.registerStatus(),
      outlookGateway.poolStats(),
      outlookGateway.poolAccounts({ category: poolCategory.value, keyword: poolSearch.value, page: 1, page_size: 200 }),
      outlookGateway.oauthCheckConfig(),
    ])
    rows.value = page.items
    migration.value = migrationResult.summary || migrationResult
    resultRows.value = result
    proxies.value = proxyPage.items
    proxyTotal.value = proxyPage.total
    proxyGroups.value = groups
    registerStatus.value = status
    acceptRegisterConfig(status.config, false, configRevision)
    poolStats.value = poolStatsResult.stats
    poolRows.value = poolPage.items
    poolCheckConfig.value = poolConfigResult.config
    registerLogs.value = (await outlookGateway.registerLogs()).items
    if (selected.value) selected.value = rows.value.find((item) => item.id === selected.value?.id) || null
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : 'Outlook 数据读取失败')
  } finally { loading.value = false }
}

async function check(row: OutlookAccount, kind: 'oauth' | 'graph') {
  checkingId.value = `${row.id}:${kind}`
  try {
    const result = kind === 'oauth' ? await outlookGateway.checkOauth(row.id) : await outlookGateway.checkGraph(row.id)
    ElMessage[result.ok ? 'success' : 'warning'](result.ok ? `${kind === 'oauth' ? 'OAuth' : 'Graph'} 检查通过 · ${result.poolStatus}` : (result.error || '检查失败'))
    await refresh()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '检查失败') }
  finally { checkingId.value = '' }
}

async function loadMessages(row: OutlookAccount) {
  selected.value = row
  try { messages.value = (await outlookGateway.messages(row.id)).messages }
  catch (error) { messages.value = []; ElMessage.error(error instanceof Error ? error.message : 'Graph 邮件读取失败') }
}

async function showMessage(row: OutlookMessage) {
  if (!selected.value) return
  try {
    const result = await outlookGateway.message(selected.value.id, row.id)
    await ElMessageBox.alert(`<pre class="message-body">${escapeHtml(result.message.body || result.message.preview || '（无正文）')}</pre>`, row.subject || '邮件详情', { dangerouslyUseHTMLString: true, confirmButtonText: '关闭', customClass: 'outlook-message-dialog' })
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '邮件详情读取失败') }
}

function escapeHtml(value: string) {
  return value.replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char] || char)
}


async function runRegisterAction(action: 'start' | 'stop' | 'reset') {
  const configRevision = registerConfigRevision()
  if (action === 'start' && registerConfigDirty.value) {
    ElMessage.warning('请先保存配置，再启动 Outlook 任务')
    return
  }
  registerAction.value = true
  try {
    const result = action === 'start' ? await outlookGateway.startRegister() : action === 'stop' ? await outlookGateway.stopRegister() : await outlookGateway.resetRegister()
    registerStatus.value = result
    acceptRegisterConfig(result.config, false, configRevision)
    registerLogs.value = (await outlookGateway.registerLogs()).items
    const stopMessage = result.status === 'stopping' ? '已发送停止请求，等待浏览器收尾' : 'Outlook 任务已停止'
    ElMessage.success(action === 'start' ? 'Outlook 任务已启动' : action === 'stop' ? stopMessage : 'Outlook 任务已重置')
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '任务操作失败') }
  finally { registerAction.value = false }
}

async function saveRegisterConfig() {
  registerAction.value = true
  try {
    const result = await outlookGateway.updateRegisterConfig(registerConfigPayload())
    acceptRegisterConfig(result.config, true)
    ElMessage.success('Outlook 注册任务配置已保存')
    await refresh()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '配置保存失败') }
  finally { registerAction.value = false }
}

async function importAccounts() {
  const accounts = importText.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => {
    const [email = '', password = '', clientId = '', ...token] = line.split('----')
    return { email: email.trim(), password, clientId, refreshToken: token.join('----') }
  })
  if (!accounts.length) return
  importing.value = true
  try {
    const result = await outlookGateway.import(accounts)
    ElMessage.success(`导入完成：新增 ${result.imported}，重复 ${result.duplicates}，错误 ${result.errors}`)
    importText.value = ''
    await refresh()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '导入失败') }
  finally { importing.value = false }
}

async function migrateLegacy() {
  try {
    const result = await outlookGateway.migrateLegacy()
    migration.value = result
    ElMessage.success(`旧数据迁移完成：新增 ${result.imported ?? 0}，重复 ${result.duplicates ?? 0}，错误 ${result.errors ?? 0}`)
    await refresh()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '旧数据迁移失败') }
}

async function exportAccounts(ids?: string[]) {
  try {
    const content = await outlookGateway.export(ids)
    const url = URL.createObjectURL(new Blob([content], { type: 'text/plain;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = 'outlook-accounts.txt'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '导出失败') }
}

function onPoolSelection(selection: OutlookPoolItem[]) {
  poolSelectedIds.value = selection.map((item) => item.id)
}

async function refreshPool() {
  try {
    const [stats, page, config] = await Promise.all([
      outlookGateway.poolStats(),
      outlookGateway.poolAccounts({ category: poolCategory.value, keyword: poolSearch.value, page: 1, page_size: 200 }),
      outlookGateway.oauthCheckConfig(),
    ])
    poolStats.value = stats.stats
    poolRows.value = page.items
    poolCheckConfig.value = config.config
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '邮箱池读取失败') }
}

async function generateSubEmails(row?: OutlookPoolItem) {
  const ids = row ? [row.id] : poolSelectedIds.value
  if (!ids.length) { ElMessage.warning('请先选择 Outlook 主账号') ; return }
  poolBusy.value = true
  try {
    const result = row
      ? await outlookGateway.generateSubEmails(row.id, poolSubCount.value, poolTagPrefix.value)
      : await outlookGateway.batchGenerateSubEmails(ids, poolSubCount.value, poolTagPrefix.value)
    const created = Array.isArray((result as any).created)
      ? (result as any).created.length
      : Number((result as any).success ?? (result as any).created ?? 0)
    const failed = Number((result as any).failed ?? 0)
    ElMessage[failed ? 'warning' : 'success'](`子邮箱已生成 ${created} 个${failed ? `，失败 ${failed} 个` : ''}`)
    await refreshPool()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '子邮箱生成失败') }
  finally { poolBusy.value = false }
}

async function checkPoolOauth() {
  if (!poolSelectedIds.value.length) { ElMessage.warning('请先选择需要检查的邮箱') ; return }
  poolBusy.value = true
  try {
    const result = await outlookGateway.batchCheckPoolOauth(poolSelectedIds.value)
    const count = result.total ?? result.processed ?? result.checked ?? poolSelectedIds.value.length
    if (result.ok === false) ElMessage.warning(result.error || 'OAuth 检查未完成')
    else ElMessage.success(`OAuth 检查完成：${count} 个`)
    await refreshPool()
    await refresh()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : 'OAuth 检查失败') }
  finally { poolBusy.value = false }
}

async function savePoolCheckConfig() {
  if (!poolCheckConfig.value) return
  poolBusy.value = true
  try {
    const result = await outlookGateway.updateOauthCheckConfig({ enabled: poolCheckConfig.value.enabled, interval_sec: poolCheckConfig.value.interval_sec, delay_ms: poolCheckConfig.value.delay_ms })
    poolCheckConfig.value = result.config
    ElMessage.success('OAuth 定时检查配置已保存')
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : 'OAuth 定时配置保存失败') }
  finally { poolBusy.value = false }
}

async function deletePoolSelection() {
  if (!poolSelectedIds.value.length) { ElMessage.warning('请先选择可删除的邮箱') ; return }
  try {
    await ElMessageBox.confirm(`删除已选择的 ${poolSelectedIds.value.length} 项？已分配或已预留项目会由服务拒绝。`, '删除邮箱池项目', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' })
    poolBusy.value = true
    const result = await outlookGateway.deletePoolItems(poolSelectedIds.value)
    ElMessage.success(`已删除 ${result.deleted} 项`)
    poolSelectedIds.value = []
    await refreshPool()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error instanceof Error ? error.message : '删除失败')
  } finally { poolBusy.value = false }
}

async function exportPool(category: string) {
  try {
    const text = await outlookGateway.exportPool(category, poolSelectedIds.value)
    const link = document.createElement('a')
    link.href = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }))
    link.download = `outlook-${category}.txt`
    link.click()
    URL.revokeObjectURL(link.href)
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '邮箱池导出失败') }
}

function poolToken(row: OutlookPoolItem) {
  const value = row.receiveUrl || ''
  return value.split('/').filter(Boolean).pop() || ''
}

async function loadPoolOtp(row: OutlookPoolItem) {
  const token = poolToken(row)
  if (!token) return
  try {
    const result = await outlookGateway.receive(token)
    poolOtp.value[row.id] = result.latest_code || (result.codes || [])[0] || '暂无验证码'
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '验证码读取失败') }
}

async function copyPoolUrl(row: OutlookPoolItem) {
  if (!row.receiveUiUrl) return
  try {
    await navigator.clipboard.writeText(row.receiveUiUrl)
    ElMessage.success('接码地址已复制')
  } catch {
    await ElMessageBox.alert(escapeHtml(row.receiveUiUrl), '接码地址（请手动复制）', { dangerouslyUseHTMLString: true, confirmButtonText: '关闭' })
  }
}

async function editAccount(row: OutlookAccount) {
  const fields: Array<['password' | 'clientId' | 'refreshToken', string, string]> = [
    ['password', '登录密码', '留空则保持原值'],
    ['clientId', 'OAuth Client ID', row.hasClientId ? '留空则保持原值' : '输入 Client ID'],
    ['refreshToken', 'OAuth Refresh Token', row.hasRefreshToken ? '留空则保持原值' : '输入 Refresh Token'],
  ]
  const changes: Record<string, string> = {}
  try {
    for (const [key, title, placeholder] of fields) {
      const result = await ElMessageBox.prompt(`${row.email} · ${title}`, '编辑 Outlook 账号', {
        inputType: key === 'password' || key === 'refreshToken' ? 'password' : 'text',
        inputPlaceholder: placeholder,
        inputValue: '',
        confirmButtonText: '下一项',
        cancelButtonText: '取消',
        distinguishCancelAndClose: true,
      })
      if (result.value.trim()) changes[key === 'clientId' ? 'clientId' : key === 'refreshToken' ? 'refreshToken' : 'password'] = result.value.trim()
    }
    if (!Object.keys(changes).length) return
    await outlookGateway.update(row.id, changes)
    ElMessage.success('账号已更新；OAuth/Graph 凭据变更后需要重新验证')
    await refresh()
  } catch { /* dialog cancellation is expected */ }
}

async function removeAccount(row: OutlookAccount) {
  try {
    await ElMessageBox.confirm(`删除 ${row.email}？仅未分配且未预留的邮箱可删除。`, '删除 Outlook 账号', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' })
    await outlookGateway.remove(row.id)
    ElMessage.success('Outlook 账号已删除')
    if (selected.value?.id === row.id) { selected.value = null; messages.value = [] }
    await refresh()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error instanceof Error ? error.message : '删除失败')
  }
}

onMounted(() => {
  void refresh()
  taskTimer = window.setInterval(() => {
    if (registerStatus.value?.enabled || registerStatus.value?.status === 'stopping') void refreshTaskState()
  }, 2000)
})

onUnmounted(() => {
  if (taskTimer !== undefined) window.clearInterval(taskTimer)
})
</script>

<template>
  <section class="outlook-view">
    <div class="page-heading">
      <div><h2>Outlook 账号管理</h2><p>单服务使用主 MongoDB；普通邮箱 API 不返回 Outlook OAuth 凭据。</p></div>
      <div class="toolbar">
        <el-button :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button>
        <el-button :icon="Download" @click="exportAccounts()">导出凭据</el-button>
      </div>
    </div>

    <el-alert type="info" :closable="false" show-icon :title="migration ? `迁移：新增 ${migration.imported ?? 0} · 重复 ${migration.duplicates ?? 0} · 错误 ${migration.errors ?? 0}；原文件保留只读备份` : '旧 Outlook 文件将在主服务启动时按邮箱幂等迁移；未经 OAuth 与 Graph 验证不会发布。'">
      <template #default><div class="migration"><span>{{ registrationSummary }} · 代理组 {{ registerStatus?.proxyGroup || '默认组' }} · 可用代理 {{ registerStatus?.proxyCount || 0 }}</span><el-button size="small" @click="migrateLegacy">重复执行迁移</el-button></div></template>
    </el-alert>

    <el-card shadow="never">
      <template #header><div class="card-header"><strong>Outlook 注册与授权任务</strong><span class="muted">独立于 GPT 任务；按执行模式运行浏览器注册、OAuth/Graph 校验，并将结果写入 MongoDB 邮箱池</span></div></template>
      <div class="task-actions">
        <el-button type="primary" :loading="registerAction" :disabled="registerStatus?.enabled" @click="runRegisterAction('start')">启动</el-button>
        <el-button :loading="registerAction" :disabled="!registerStatus?.enabled" @click="runRegisterAction('stop')">停止</el-button>
        <el-button :loading="registerAction" @click="runRegisterAction('reset')">重置</el-button>
        <span class="muted">状态：{{ registerStatus?.status || 'idle' }} · 日志 {{ registerStatus?.log_count || 0 }} 条</span>
      </div>
      <p v-if="registerConfigDirty" role="status">有未保存的配置；轮询会保留草稿，启动前请先保存。</p>
      <el-form inline label-width="90px" class="task-config" :disabled="registerAction">
        <el-form-item label="执行模式">
          <el-select v-model="registerConfig.execution_mode" style="width: 180px">
            <el-option label="自动选择" value="auto" />
            <el-option label="仅注册引擎" value="registration" />
            <el-option label="仅校验 OAuth" value="authorized" />
            <el-option label="注册后再校验" value="both" />
          </el-select>
        </el-form-item>
        <el-form-item label="任务数"><el-input-number v-model="registerConfig.tasks" :min="1" :max="100000" /></el-form-item>
        <el-form-item label="并发"><el-input-number v-model="registerConfig.concurrent_flows" :min="1" :max="64" /></el-form-item>
        <el-form-item label="无头"><el-switch v-model="registerConfig.headless" /></el-form-item>
        <el-form-item label="代理分组"><el-input v-model="registerConfig.proxy.group" placeholder="默认组" /></el-form-item>
        <OutlookOAuthConfig v-model="registerConfig.oauth2" />
        <el-form-item><el-button type="success" :loading="registerAction" @click="saveRegisterConfig">保存配置</el-button></el-form-item>
      </el-form>
      <el-table :data="registerLogs" size="small" max-height="180" empty-text="暂无任务日志">
        <el-table-column prop="createdAt" label="时间" width="220" /><el-table-column prop="level" label="级别" width="90" /><el-table-column prop="line" label="日志" min-width="360" show-overflow-tooltip />
      </el-table>
    </el-card>

    <el-card shadow="never">
      <template #header><div class="card-header"><strong>导入 Outlook 账号</strong><span class="muted">每行 email----password----client_id----refresh_token</span></div></template>
      <el-input v-model="importText" type="textarea" :rows="3" autocomplete="off" placeholder="name@outlook.com----password----client_id----refresh_token" />
      <div class="form-actions"><el-button type="primary" :icon="UploadFilled" :loading="importing" @click="importAccounts">导入到主 MongoDB</el-button><span class="muted">凭据仅进入 Outlook 专用集合，不显示在邮箱池列表</span></div>
    </el-card>

    <el-card shadow="never">
      <template #header><div class="card-header"><strong>主代理池（共享）</strong><span class="muted">{{ proxyTotal }} 个代理 · {{ proxyGroupCount }} 个分组；与 GPT 控制台共用同一 Mongo 清单和分组</span></div></template>
      <el-table :data="proxyGroups" size="small" empty-text="主代理池暂无分组">
        <el-table-column prop="country" label="国家" width="90" />
        <el-table-column prop="group" label="代理分组" min-width="180" />
        <el-table-column prop="total" label="总数" width="90" />
        <el-table-column prop="enabled" label="启用" width="90" />
        <el-table-column prop="available" label="可用" width="90" />
      </el-table>
      <el-table :data="proxies" size="small" class="proxy-table" empty-text="主代理池暂无代理">
        <el-table-column prop="country" label="国家" width="80" />
        <el-table-column prop="group" label="分组" width="150" />
        <el-table-column label="代理地址" min-width="230"><template #default="{ row }">{{ row.scheme }}://{{ row.host }}:{{ row.port }}</template></el-table-column>
        <el-table-column prop="status" label="状态" width="110" />
        <el-table-column label="启用" width="80"><template #default="{ row }">{{ row.enabled ? '是' : '否' }}</template></el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never" class="pool-card">
      <template #header>
        <div class="card-header"><strong>Outlook 邮箱池与接码</strong><span class="muted">运行数据来自 MongoDB；旧 Results 文件不参与运行时读取</span></div>
      </template>
      <div class="pool-stats">
        <el-tag>总数 {{ poolStats?.total || 0 }}</el-tag>
        <el-tag type="success">已注册 {{ poolStats?.registered || 0 }}</el-tag>
        <el-tag type="success">OAuth2 {{ poolStats?.oauth2 || 0 }}</el-tag>
        <el-tag type="warning">子邮箱 {{ poolStats?.sub || 0 }}</el-tag>
        <el-tag type="danger">OAuth 异常 {{ poolStats?.oauth_bad || 0 }}</el-tag>
      </div>
      <div class="pool-toolbar">
        <el-select v-model="poolCategory" style="width: 150px" @change="refreshPool">
          <el-option label="全部" value="all" /><el-option label="已注册" value="registered" /><el-option label="OAuth2" value="oauth2" /><el-option label="子邮箱" value="sub" /><el-option label="已绑定恢复邮箱" value="recovery" />
        </el-select>
        <el-input v-model="poolSearch" clearable style="width: 240px" placeholder="搜索邮箱" @keyup.enter="refreshPool" />
        <el-input-number v-model="poolSubCount" :min="1" :max="50" />
        <el-input v-model="poolTagPrefix" style="width: 140px" placeholder="子邮箱标签前缀" />
        <el-button type="primary" :loading="poolBusy" @click="generateSubEmails()">批量生成子邮箱</el-button>
        <el-button :loading="poolBusy" @click="checkPoolOauth">批量 OAuth 检查</el-button>
        <el-button :loading="poolBusy" @click="deletePoolSelection">删除选择</el-button>
        <el-dropdown @command="exportPool">
          <el-button :icon="Download">导出</el-button>
          <template #dropdown><el-dropdown-menu><el-dropdown-item command="registered">已注册</el-dropdown-item><el-dropdown-item command="oauth2">OAuth2</el-dropdown-item><el-dropdown-item command="sub">子邮箱</el-dropdown-item><el-dropdown-item command="recovery">恢复邮箱</el-dropdown-item></el-dropdown-menu></template>
        </el-dropdown>
      </div>
      <div v-if="poolCheckConfig" class="pool-check-config">
        <el-switch v-model="poolCheckConfig.enabled" active-text="启用定时 OAuth 检查" />
        <el-input-number v-model="poolCheckConfig.interval_sec" :min="300" :max="604800" />
        <span class="muted">秒间隔</span>
        <el-button size="small" :loading="poolBusy" @click="savePoolCheckConfig">保存定时配置</el-button>
        <span class="muted">{{ poolCheckConfig.last_result ? `上次：${JSON.stringify(poolCheckConfig.last_result)}` : '尚未运行' }}</span>
      </div>
      <el-table :data="poolRows" size="small" row-key="id" @selection-change="onPoolSelection" empty-text="邮箱池暂无数据">
        <el-table-column type="selection" width="44" />
        <el-table-column prop="category" label="分类" width="95" />
        <el-table-column prop="email" label="邮箱" min-width="220" />
        <el-table-column prop="parentEmail" label="主账号" min-width="200" />
        <el-table-column prop="oauthStatus" label="OAuth" width="100" />
        <el-table-column prop="status" label="状态" width="100" />
        <el-table-column label="接码" min-width="250">
          <template #default="{ row }">
            <template v-if="row.receiveUrl">
              <el-button size="small" @click="loadPoolOtp(row)">查 OTP</el-button>
              <el-button size="small" @click="copyPoolUrl(row)">复制地址</el-button>
              <span class="otp-value">{{ poolOtp[row.id] || '—' }}</span>
            </template>
            <span v-else class="muted">主账号</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.category !== 'sub'" size="small" @click="generateSubEmails(row)">生成子邮箱</el-button>
            <el-button v-if="row.receiveUiUrl" size="small" @click="copyPoolUrl(row)">接码地址</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <div class="toolbar filters">
      <el-input v-model="search" clearable placeholder="搜索 Outlook 邮箱" @keyup.enter="refresh" />
      <el-select v-model="filter" aria-label="Outlook 邮箱池状态" @change="refresh">
        <el-option label="全部状态" value="all" /><el-option label="可分配" value="available" />
        <el-option label="已预留" value="reserved" /><el-option label="已分配" value="assigned" />
        <el-option label="未发布 / 冲突" value="not_published" /><el-option label="不可用" value="unavailable" />
      </el-select>
    </div>

    <el-table v-loading="loading" :data="rows" row-key="id" empty-text="暂无 Outlook 账号" @row-click="loadMessages">
      <el-table-column prop="email" label="邮箱" min-width="220" />
      <el-table-column label="来源" width="110"><template #default="{ row }"><el-tag effect="plain">{{ row.source === 'migration' ? '旧数据迁移' : row.source === 'registration' ? 'Outlook 注册' : '手动导入' }}</el-tag></template></el-table-column>
      <el-table-column label="OAuth" width="115"><template #default="{ row }"><el-tag :type="statusType(row.oauthStatus)">{{ row.oauthStatus }}</el-tag></template></el-table-column>
      <el-table-column label="Graph" width="115"><template #default="{ row }"><el-tag :type="statusType(row.graphStatus)">{{ row.graphStatus }}</el-tag></template></el-table-column>
      <el-table-column label="分配状态" width="125"><template #default="{ row }"><el-tag :type="statusType(row.poolStatus)">{{ row.poolStatus }}</el-tag></template></el-table-column>
      <el-table-column label="凭据" width="95"><template #default="{ row }">{{ row.hasClientId && row.hasRefreshToken ? '已配置' : '待补全' }}</template></el-table-column>
      <el-table-column label="操作" width="300" fixed="right"><template #default="{ row }">
        <el-button size="small" :loading="checkingId === `${row.id}:oauth`" @click.stop="check(row, 'oauth')">OAuth</el-button>
        <el-button size="small" type="primary" :loading="checkingId === `${row.id}:graph`" @click.stop="check(row, 'graph')">Graph</el-button>
        <el-button size="small" :icon="Edit" aria-label="编辑 Outlook 账号" @click.stop="editAccount(row)" />
        <el-button size="small" type="danger" :icon="Delete" aria-label="删除 Outlook 账号" @click.stop="removeAccount(row)" />
      </template></el-table-column>
    </el-table>

    <el-card shadow="never">
      <template #header><div class="card-header"><strong>Outlook 结果记录</strong><span class="muted">不包含密码、Client ID 或 Token</span></div></template>
      <el-table :data="resultRows" size="small" empty-text="暂无结果记录">
        <el-table-column prop="email" label="邮箱" min-width="220" />
        <el-table-column prop="oauthStatus" label="OAuth" width="130" />
        <el-table-column prop="graphStatus" label="Graph" width="130" />
        <el-table-column prop="createdAt" label="创建时间" min-width="200" />
      </el-table>
    </el-card>

    <el-card v-if="selected" shadow="never">
      <template #header><div class="card-header"><strong><el-icon><Message /></el-icon> {{ selected.email }} · Graph 收件箱</strong><el-button size="small" @click="loadMessages(selected)">刷新邮件</el-button></div></template>
      <el-table :data="messages" empty-text="收件箱为空" @row-click="showMessage">
        <el-table-column prop="subject" label="主题（点击查看正文）" min-width="240" />
        <el-table-column prop="from" label="发件人" min-width="210" />
        <el-table-column prop="receivedAt" label="接收时间" width="200" />
        <el-table-column prop="preview" label="摘要" min-width="300" show-overflow-tooltip />
      </el-table>
    </el-card>
  </section>
</template>

<style scoped>
.outlook-view { display: grid; gap: 16px; }
.toolbar, .card-header, .migration, .form-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.filters { justify-content: flex-start; }
.filters :deep(.el-input) { width: min(360px, 70vw); }
.filters :deep(.el-select) { width: 210px; }
.form-actions { justify-content: flex-start; margin-top: 12px; }
.task-actions, .task-config, .pool-toolbar, .pool-check-config, .pool-stats { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.pool-card :deep(.el-table) { margin-top: 12px; }
.pool-stats { margin-bottom: 0; }
.otp-value { margin-left: 6px; font-weight: 700; color: var(--el-color-danger); }
.proxy-table { margin-top: 12px; }
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
</style>
