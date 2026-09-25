<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Delete, Download, Edit, Message, Refresh, UploadFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { outlookGateway, type OutlookAccount, type OutlookMessage, type OutlookProxy, type OutlookRegisterSnapshot } from '@/services/outlookGateway'
import type { ProxyGroupSummary } from '@/types'

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
const registerConfig = ref<Record<string, any>>({})

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

async function refresh() {
  loading.value = true
  try {
    const [page, migrationResult, result, proxyPage, groups, status] = await Promise.all([
      outlookGateway.list({ page: 1, pageSize: 200, q: search.value, poolStatus: filter.value }),
      outlookGateway.migration(),
      outlookGateway.results(100, search.value),
      outlookGateway.proxies(1, 100),
      outlookGateway.proxyGroups(),
      outlookGateway.registerStatus(),
    ])
    rows.value = page.items
    migration.value = migrationResult.summary || migrationResult
    resultRows.value = result
    proxies.value = proxyPage.items
    proxyTotal.value = proxyPage.total
    proxyGroups.value = groups
    registerStatus.value = status
    registerConfig.value = status.config || {}
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
  registerAction.value = true
  try {
    const result = action === 'start' ? await outlookGateway.startRegister() : action === 'stop' ? await outlookGateway.stopRegister() : await outlookGateway.resetRegister()
    registerStatus.value = result
    registerConfig.value = result.config || registerConfig.value
    registerLogs.value = (await outlookGateway.registerLogs()).items
    ElMessage.success(action === 'start' ? 'Outlook 任务已启动' : action === 'stop' ? 'Outlook 任务已停止' : 'Outlook 任务已重置')
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '任务操作失败') }
  finally { registerAction.value = false }
}

async function saveRegisterConfig() {
  registerAction.value = true
  try {
    const result = await outlookGateway.updateRegisterConfig(registerConfig.value)
    registerConfig.value = result.config
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

onMounted(() => void refresh())
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
      <template #header><div class="card-header"><strong>Outlook 授权账号任务</strong><span class="muted">独立于 GPT 任务；处理主 MongoDB 中已有授权账号，执行 OAuth/Graph 校验并发布邮箱池</span></div></template>
      <div class="task-actions">
        <el-button type="primary" :loading="registerAction" :disabled="registerStatus?.enabled" @click="runRegisterAction('start')">启动</el-button>
        <el-button :loading="registerAction" :disabled="!registerStatus?.enabled" @click="runRegisterAction('stop')">停止</el-button>
        <el-button :loading="registerAction" @click="runRegisterAction('reset')">重置</el-button>
        <span class="muted">状态：{{ registerStatus?.status || 'idle' }} · 日志 {{ registerStatus?.log_count || 0 }} 条</span>
      </div>
      <el-form inline label-width="90px" class="task-config">
        <el-form-item label="任务数"><el-input-number v-model="registerConfig.tasks" :min="1" :max="100000" /></el-form-item>
        <el-form-item label="并发"><el-input-number v-model="registerConfig.concurrent_flows" :min="1" :max="64" /></el-form-item>
        <el-form-item label="无头"><el-switch v-model="registerConfig.headless" /></el-form-item>
        <el-form-item label="代理分组"><el-input v-model="registerConfig.proxy.group" placeholder="默认组" /></el-form-item>
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
.task-actions, .task-config { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.proxy-table { margin-top: 12px; }
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
</style>
