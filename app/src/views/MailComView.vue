<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Delete, Download, Link, Message, Refresh, Search, UploadFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { mailcomGateway, type MailComAccount, type MailComAlias, type MailComMessage, type MailComMigrationResult } from '@/services/mailcomGateway'

const accounts = ref<MailComAccount[]>([])
const aliases = ref<MailComAlias[]>([])
const messages = ref<MailComMessage[]>([])
const selected = ref<MailComAccount | null>(null)
const loading = ref(false)
const action = ref(false)
const importing = ref(false)
const query = ref('')
const importText = ref('')
const aliasEmail = ref('')
const aliasLabel = ref('')
const page = ref(1)
const pageSize = 50
const total = ref(0)
const migration = ref<MailComMigrationResult | null>(null)
const latestCode = ref<string | null>(null)
const health = ref('检查中')
const mailFolder = ref('INBOX')
const server = ref({ host: '', port: 22, username: '', password: '' })
const serverSyncing = ref(false)
const pages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

async function loadAccounts() {
  loading.value = true
  try {
    const result = await mailcomGateway.accounts(page.value, pageSize, query.value)
    accounts.value = result.items
    total.value = result.total
    if (selected.value) {
      selected.value = accounts.value.find((row) => row.id === selected.value?.id) ?? null
      if (!selected.value) { aliases.value = []; messages.value = [] }
    }
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '读取 MailCom 账号失败') }
  finally { loading.value = false }
}

async function loadHealth() {
  try { const result = await mailcomGateway.health(); health.value = `${result.storage} · 主服务` }
  catch { health.value = '不可用' }
}

async function chooseAccount(row: MailComAccount) {
  selected.value = row
  messages.value = []
  latestCode.value = null
  try { aliases.value = (await mailcomGateway.aliases(row.id)).items }
  catch (error) { ElMessage.error(error instanceof Error ? error.message : '读取别名失败') }
}

async function importAccounts() {
  if (!importText.value.trim()) return
  importing.value = true
  try {
    const result = await mailcomGateway.importAccounts(importText.value)
    importText.value = ''
    ElMessage.success(`处理 ${result.total} 项：导入 ${result.imported}，重复 ${result.duplicateCount}，错误 ${result.errorCount}`)
    page.value = 1
    await loadAccounts()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '导入失败') }
  finally { importing.value = false }
}

async function removeAccount(row: MailComAccount) {
  try {
    await ElMessageBox.confirm(`删除 ${row.email} 及其所有别名？此操作不可自动恢复。`, '删除 MailCom 账号', { type: 'warning' })
    await mailcomGateway.deleteAccount(row.id)
    if (selected.value?.id === row.id) { selected.value = null; aliases.value = []; messages.value = [] }
    ElMessage.success('账号和别名已删除')
    await loadAccounts()
  } catch (error) { if (error !== 'cancel' && error !== 'close') ElMessage.error(error instanceof Error ? error.message : '删除失败') }
}

async function addAlias() {
  if (!selected.value || !aliasEmail.value.trim()) return
  action.value = true
  try {
    const result = await mailcomGateway.importAlias(selected.value.id, aliasEmail.value.trim(), aliasLabel.value.trim())
    if (result.status === 'duplicate') ElMessage.warning('别名已存在')
    else { ElMessage.success('别名已添加'); aliasEmail.value = ''; aliasLabel.value = '' }
    await chooseAccount(selected.value)
    await loadAccounts()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '添加别名失败') }
  finally { action.value = false }
}

async function removeAlias(alias: MailComAlias) {
  try {
    await ElMessageBox.confirm(`删除别名 ${alias.email}？`, '删除别名', { type: 'warning' })
    await mailcomGateway.deleteAlias(alias.id)
    ElMessage.success('别名已删除')
    if (selected.value) await chooseAccount(selected.value)
    await loadAccounts()
  } catch (error) { if (error !== 'cancel' && error !== 'close') ElMessage.error(error instanceof Error ? error.message : '删除别名失败') }
}

async function testImap(row: MailComAccount) {
  action.value = true
  try {
    const result = await mailcomGateway.testAccount(row.id)
    if (result.ok) ElMessage.success(`IMAP 连通，${result.messageCount ?? 0} 封邮件`)
    else ElMessage.error(result.error?.message || 'IMAP 连通性失败')
    await loadAccounts()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : 'IMAP 测试失败') }
  finally { action.value = false }
}

async function loadMessages() {
  if (!selected.value) return
  action.value = true
  latestCode.value = null
  try { messages.value = (await mailcomGateway.messages(selected.value.id, mailFolder.value)).items }
  catch (error) { ElMessage.error(error instanceof Error ? error.message : '读取邮件失败') }
  finally { action.value = false }
}

async function queryCode() {
  if (!selected.value) return
  action.value = true
  try {
    const result = await mailcomGateway.latestCode(selected.value.id)
    latestCode.value = result.message?.verificationCode ?? null
    if (latestCode.value) ElMessage.success(`最新验证码：${latestCode.value}`)
    else ElMessage.info('未找到验证码邮件')
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '验证码查询失败') }
  finally { action.value = false }
}

async function runMigration() {
  action.value = true
  try {
    migration.value = await mailcomGateway.migrate()
    ElMessage.success(migration.value.status === 'missing' ? '未发现旧 SQLite 数据库' : `迁移完成：新增 ${migration.value.imported ?? 0}，重复 ${migration.value.duplicates ?? 0}，错误 ${migration.value.errors ?? 0}`)
    await loadAccounts()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : 'SQLite 迁移失败') }
  finally { action.value = false }
}

async function pushServerSnapshot() {
  serverSyncing.value = true
  try {
    const result = await mailcomGateway.serverSync(server.value)
    server.value.password = ''
    ElMessage.success(`服务器快照已推送：账号 ${result.accounts}，别名 ${result.aliases}；Host key SHA-256 ${result.hostKeySha256}`)
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : '服务器推送失败') }
  finally { serverSyncing.value = false }
}

onMounted(() => { void loadAccounts(); void loadHealth() })
</script>

<template>
  <section class="mailcom-view">
    <el-card shadow="never">
      <template #header><div class="head"><div><strong>MailCom Hub · 主服务</strong><div class="muted">MongoDB 存储 · {{ health }} · 旧 3211 管理器仅用于迁移与回滚</div></div><el-button :icon="Refresh" :loading="loading" @click="loadAccounts">刷新</el-button></div></template>
      <div class="toolbar"><el-input v-model="query" clearable placeholder="搜索邮箱" :prefix-icon="Search" @keyup.enter="page = 1; loadAccounts()" @clear="page = 1; loadAccounts()" /><el-button type="primary" @click="page = 1; loadAccounts()">搜索</el-button><el-button :icon="Download" :loading="action" @click="runMigration">迁移旧 SQLite</el-button><el-tag>共 {{ total }} 个主账号</el-tag></div>
      <el-alert v-if="migration" class="migration-result" :type="migration.errors || migration.conflicts ? 'warning' : 'success'" :closable="false" :title="migration.status === 'missing' ? '没有发现旧 MailCom SQLite 数据库' : `SQLite 迁移：账号 ${migration.accounts ?? 0}、别名 ${migration.aliases ?? 0}、新增 ${migration.imported ?? 0}、重复 ${migration.duplicates ?? 0}、冲突 ${migration.conflicts ?? 0} · 错误 ${migration.errors ?? 0}`" />
      <el-table v-loading="loading" :data="accounts" highlight-current-row @row-click="chooseAccount">
        <el-table-column prop="email" label="邮箱" min-width="240" />
        <el-table-column label="状态" width="130"><template #default="scope"><el-tag :type="scope.row.status === 'online' ? 'success' : scope.row.status === 'failed' ? 'danger' : 'info'">{{ scope.row.status }}</el-tag></template></el-table-column>
        <el-table-column prop="aliasCount" label="别名" width="90" />
        <el-table-column prop="messageCount" label="邮件数" width="100" />
        <el-table-column prop="lastCheckedAt" label="最近检查" min-width="190" />
        <el-table-column prop="lastError" label="最近错误" min-width="180" show-overflow-tooltip />
        <el-table-column label="操作" width="210" fixed="right"><template #default="scope"><el-button size="small" :loading="action" @click.stop="testImap(scope.row)">IMAP 测试</el-button><el-button size="small" type="danger" :icon="Delete" @click.stop="removeAccount(scope.row)" /></template></el-table-column>
      </el-table>
      <div class="pager"><el-button :disabled="page <= 1" @click="page--; loadAccounts()">上一页</el-button><span>{{ page }} / {{ pages }}</span><el-button :disabled="page >= pages" @click="page++; loadAccounts()">下一页</el-button></div>
    </el-card>

    <el-card shadow="never">
      <template #header><strong><el-icon><UploadFilled /></el-icon> 导入 MailCom 主账号</strong></template>
      <p class="muted">每行格式：邮箱----密码。密码仅在提交时发送，导入后立即清除输入内容。</p>
      <el-input v-model="importText" type="textarea" :rows="4" autocomplete="off" placeholder="name@mail.com----PASSWORD" />
      <div class="actions"><el-button type="primary" :loading="importing" @click="importAccounts">导入账号</el-button><el-button @click="importText = ''">清空</el-button></div>
    </el-card>

    <el-card v-if="selected" shadow="never">
      <template #header><div class="head"><strong>{{ selected.email }} · 别名与收件箱</strong><el-tag>{{ aliases.length }} 个别名</el-tag></div></template>
      <div class="alias-form"><el-input v-model="aliasEmail" placeholder="alias@mail.com" /><el-input v-model="aliasLabel" placeholder="备注（可选）" /><el-button type="primary" :loading="action" @click="addAlias">添加别名</el-button></div>
      <el-table :data="aliases" empty-text="没有别名"><el-table-column prop="email" label="别名邮箱" min-width="240" /><el-table-column prop="label" label="备注" min-width="160" /><el-table-column prop="createdAt" label="创建时间" min-width="190" /><el-table-column label="操作" width="90"><template #default="scope"><el-button type="danger" link :icon="Delete" @click="removeAlias(scope.row)">删除</el-button></template></el-table-column></el-table>
      <div class="mail-tools"><el-select v-model="mailFolder" aria-label="邮件文件夹"><el-option label="收件箱" value="INBOX" /><el-option label="Spam" value="Spam" /><el-option label="Junk" value="Junk" /></el-select><el-button :icon="Message" :loading="action" @click="loadMessages">读取邮件</el-button><el-button :loading="action" @click="queryCode">查询最新验证码</el-button><el-tag v-if="latestCode" type="success">验证码：{{ latestCode }}</el-tag></div>
      <el-table :data="messages" empty-text="尚未读取邮件"><el-table-column prop="subject" label="主题" min-width="220" /><el-table-column prop="sender" label="发件人" min-width="220" /><el-table-column prop="receivedAt" label="时间" min-width="190" /><el-table-column prop="verificationCode" label="验证码" width="110" /><el-table-column prop="preview" label="摘要" min-width="280" show-overflow-tooltip /></el-table>
    </el-card>

    <el-card shadow="never">
      <template #header><strong><el-icon><Link /></el-icon> 服务器快照推送</strong></template>
      <p class="muted">凭据只在本次请求中使用；服务响应只包含账号/别名计数与服务器 Host key 指纹。</p>
      <div class="alias-form"><el-input v-model="server.host" placeholder="服务器主机" /><el-input-number v-model="server.port" :min="1" :max="65535" /><el-input v-model="server.username" placeholder="服务器用户名" /><el-input v-model="server.password" type="password" show-password autocomplete="new-password" placeholder="服务器密码" /><el-button type="primary" :loading="serverSyncing" @click="pushServerSnapshot">推送快照</el-button></div>
    </el-card>
  </section>
</template>

<style scoped>
.mailcom-view { display: grid; gap: 16px; }
.head, .toolbar, .alias-form, .mail-tools, .actions, .pager { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.head { justify-content: space-between; }
.toolbar { margin-bottom: 12px; }
.toolbar :deep(.el-input) { width: min(320px, 60vw); }
.alias-form { margin: 12px 0; }
.alias-form :deep(.el-input) { width: min(280px, 70vw); }
.mail-tools, .actions { margin: 12px 0; }
.pager { justify-content: center; margin-top: 14px; }
.muted { color: var(--el-text-color-secondary); font-size: 12px; }
.migration-result { margin-bottom: 12px; }
</style>
