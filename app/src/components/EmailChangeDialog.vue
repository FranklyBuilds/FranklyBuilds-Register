<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { dataGateway } from '@/services/dataGateway'
import type { AccountRecord, EmailChangeRun, EmailRecord } from '@/types'

const TERMINAL_STATUSES = new Set<EmailChangeRun['status']>([
  'completed',
  'failed',
  'cancelled',
])

const props = defineProps<{
  modelValue: boolean
  account: AccountRecord | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  completed: [run: EmailChangeRun]
  'active-change': [accountId: string | null]
}>()

const targets = ref<EmailRecord[]>([])
const targetId = ref('')
const loadingTargets = ref(false)
const submitting = ref(false)
const run = ref<EmailChangeRun | null>(null)
const errorMessage = ref('')
let pollTimer: ReturnType<typeof setInterval> | undefined
let pollInFlight = false
let pollRequestToken = 0
let pollGeneration = 0
let lifecycleGeneration = 0
let loadRequestGeneration = 0
let submitRequestGeneration = 0
let cancelRequestGeneration = 0
let disposed = false

const active = computed(() => !!run.value && !TERMINAL_STATUSES.has(run.value.status))
const availableTargets = computed(() => {
  const current = normalize(props.account?.email)
  return targets.value.filter((item) => normalize(item.email) !== current)
})

function normalize(value: string | null | undefined) {
  return String(value || '').trim().toLowerCase()
}

function statusLabel(status: EmailChangeRun['status']) {
  const labels: Record<EmailChangeRun['status'], string> = {
    queued: '排队中',
    target_reserved: '目标邮箱已预留',
    remote_submitted: '正在提交远端换绑',
    remote_verified: '远端已验证',
    committing: '正在更新本地账号',
    cleanup_pending: '等待清理目标邮箱',
    completed: '换绑完成',
    failed: '换绑失败',
    cancelled: '已取消',
  }
  return labels[status] || status
}

function statusType(status: EmailChangeRun['status']) {
  if (status === 'completed') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'cancelled') return 'info'
  if (status === 'cleanup_pending') return 'warning'
  return 'primary'
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = undefined
  pollGeneration += 1
  pollInFlight = false
}

function notifyActive(accountId: string | null) {
  emit('active-change', accountId)
}

function invalidateContext() {
  lifecycleGeneration += 1
  loadRequestGeneration += 1
  submitRequestGeneration += 1
  cancelRequestGeneration += 1
  stopPolling()
  loadingTargets.value = false
  submitting.value = false
  targets.value = []
  targetId.value = ''
  errorMessage.value = ''
}

function isCurrentContext(accountId: string, generation: number) {
  return !disposed
    && props.modelValue
    && lifecycleGeneration === generation
    && props.account?.id === accountId
}

function isCurrentRequest(accountId: string, generation: number, requestGeneration: number, latest: number) {
  return isCurrentContext(accountId, generation) && requestGeneration === latest
}

async function loadTargets(accountId: string, generation: number) {
  if (!isCurrentContext(accountId, generation)) return
  const requestGeneration = ++loadRequestGeneration
  loadingTargets.value = true
  errorMessage.value = ''
  try {
    const loaded: EmailRecord[] = []
    let page = 1
    let total = 0
    do {
      const result = await dataGateway.listEmails({ page, pageSize: 100, q: '', source: 'all' })
      if (!isCurrentRequest(accountId, generation, requestGeneration, loadRequestGeneration)) return
      loaded.push(...result.items)
      total = result.total
      if (!result.items.length) break
      page += 1
    } while (loaded.length < total)
    if (!isCurrentRequest(accountId, generation, requestGeneration, loadRequestGeneration)) return
    targets.value = [...new Map(loaded.map((item) => [item.id, item])).values()]
    if (!availableTargets.value.some((item) => item.id === targetId.value)) targetId.value = ''
  } catch (error) {
    if (!isCurrentRequest(accountId, generation, requestGeneration, loadRequestGeneration)) return
    targets.value = []
    errorMessage.value = error instanceof Error ? error.message : '可用邮箱读取失败'
  } finally {
    if (isCurrentRequest(accountId, generation, requestGeneration, loadRequestGeneration)) {
      loadingTargets.value = false
    }
  }
}

interface RunContext {
  accountId: string
  generation: number
  runId: string
}

async function refreshRun(context?: RunContext) {
  const accountId = context?.accountId || props.account?.id
  const generation = context?.generation ?? lifecycleGeneration
  const runId = context?.runId || run.value?.runId
  if (!accountId || !runId || !isCurrentContext(accountId, generation)) return
  if (pollInFlight) return
  const pollToken = pollGeneration
  const requestToken = ++pollRequestToken
  pollInFlight = true
  try {
    const refreshed = await dataGateway.getEmailChange(runId)
    if (!isCurrentContext(accountId, generation) || pollToken !== pollGeneration || run.value?.runId !== runId) return
    run.value = refreshed
    if (TERMINAL_STATUSES.has(refreshed.status)) {
      stopPolling()
      notifyActive(null)
      if (refreshed.status === 'completed') emit('completed', refreshed)
    }
  } catch (error) {
    if (!isCurrentContext(accountId, generation) || pollToken !== pollGeneration || run.value?.runId !== runId) return
    // A transient poll failure should not turn a remote task into a local failure.
    errorMessage.value = error instanceof Error ? error.message : '换绑任务状态读取失败'
  } finally {
    if (pollRequestToken === requestToken) pollInFlight = false
  }
}

function startPolling(context: RunContext) {
  stopPolling()
  pollTimer = setInterval(() => void refreshRun(context), 1500)
}

async function openDialog(accountId: string, generation: number) {
  if (!isCurrentContext(accountId, generation)) return
  errorMessage.value = ''
  if (run.value && !TERMINAL_STATUSES.has(run.value.status)) {
    notifyActive(accountId)
    const context = { accountId, generation, runId: run.value.runId }
    startPolling(context)
    await refreshRun(context)
    return
  }
  if (!run.value || TERMINAL_STATUSES.has(run.value.status)) {
    run.value = null
    targetId.value = ''
    notifyActive(null)
    await loadTargets(accountId, generation)
  }
}

async function submit() {
  if (!props.account || !targetId.value || submitting.value || active.value) return
  const accountId = props.account.id
  const selectedTargetId = targetId.value
  const generation = lifecycleGeneration
  const requestGeneration = ++submitRequestGeneration
  submitting.value = true
  // Reserve the account locally while the create request is still in flight.
  // The parent uses this signal to block account deletion or opening another
  // email-change dialog during the otherwise unobservable POST window.
  notifyActive(accountId)
  errorMessage.value = ''
  try {
    const created = await dataGateway.createEmailChange(accountId, selectedTargetId)
    if (!isCurrentRequest(accountId, generation, requestGeneration, submitRequestGeneration)
      || targetId.value !== selectedTargetId) return
    run.value = created
    notifyActive(accountId)
    if (TERMINAL_STATUSES.has(created.status)) {
      notifyActive(null)
      if (created.status === 'completed') emit('completed', created)
    } else {
      const context = { accountId, generation, runId: created.runId }
      startPolling(context)
      await refreshRun(context)
    }
  } catch (error) {
    if (isCurrentRequest(accountId, generation, requestGeneration, submitRequestGeneration)
      && targetId.value === selectedTargetId) {
      errorMessage.value = error instanceof Error ? error.message : '邮箱换绑任务提交失败'
      notifyActive(null)
    }
  } finally {
    if (isCurrentContext(accountId, generation) && requestGeneration === submitRequestGeneration) {
      submitting.value = false
    }
  }
}

async function cancel() {
  if (!run.value || !active.value) return
  const accountId = props.account?.id
  if (!accountId) return
  const currentRunId = run.value.runId
  const currentTargetId = run.value.targetEmailId
  const generation = lifecycleGeneration
  const requestGeneration = ++cancelRequestGeneration
  submitting.value = true
  try {
    const cancelled = await dataGateway.cancelEmailChange(currentRunId)
    if (!isCurrentRequest(accountId, generation, requestGeneration, cancelRequestGeneration)
      || run.value?.runId !== currentRunId
      || run.value.targetEmailId !== currentTargetId) return
    run.value = cancelled
    if (TERMINAL_STATUSES.has(cancelled.status)) {
      stopPolling()
      notifyActive(null)
    }
  } catch (error) {
    if (isCurrentRequest(accountId, generation, requestGeneration, cancelRequestGeneration)) {
      errorMessage.value = error instanceof Error ? error.message : '换绑任务取消失败'
    }
  } finally {
    if (isCurrentContext(accountId, generation) && requestGeneration === cancelRequestGeneration) {
      submitting.value = false
    }
  }
}

function close(nextValue = false) {
  if (nextValue) {
    return
  }
  if (submitting.value) {
    ElMessage.warning('请求提交中，请等待结果返回')
    emit('update:modelValue', true)
    return
  }
  if (active.value) {
    ElMessage.warning('任务进行中，请先取消任务')
    emit('update:modelValue', true)
    return
  }
  invalidateContext()
  emit('update:modelValue', false)
}

let blockedCloseReopen = false

watch(
  () => [props.modelValue, props.account?.id] as const,
  ([open, accountId], previous) => {
    const [previousOpen, previousAccountId] = previous || []
    const accountChanged = accountId !== previousAccountId
    const opening = open && (previousOpen !== true || accountChanged)

    if (accountChanged) {
      // A prop swap is a new account context even when the parent closes in the same tick.
      invalidateContext()
      run.value = null
      targetId.value = ''
      targets.value = []
      errorMessage.value = ''
      blockedCloseReopen = false
      notifyActive(null)
    }

    if (!open) {
      if (submitting.value || active.value) {
        blockedCloseReopen = true
        ElMessage.warning(submitting.value ? '请求提交中，请等待结果返回' : '任务进行中，请先取消任务')
        emit('update:modelValue', true)
        return
      }
      blockedCloseReopen = false
      if (!accountChanged) invalidateContext()
      return
    }

    if (blockedCloseReopen && !accountChanged) {
      blockedCloseReopen = false
      return
    }

    if (!accountChanged && opening) {
      invalidateContext()
    }

    if (accountId && opening) void openDialog(accountId, lifecycleGeneration)
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  disposed = true
  invalidateContext()
})
</script>

<template>
  <el-dialog
    class="email-change-dialog"
    :model-value="modelValue"
    title="更换账号邮箱"
    width="min(560px, calc(100vw - 28px))"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="!active && !submitting"
    :teleported="false"
    @update:model-value="close"
  >
    <template v-if="account">
      <div class="email-change-summary">
        <span>当前邮箱</span>
        <strong>{{ account.email }}</strong>
      </div>

      <el-form label-position="top">
        <el-form-item label="目标邮箱">
          <el-select
            v-model="targetId"
            class="email-change-target"
            filterable
            :loading="loadingTargets"
            :disabled="active || submitting"
            placeholder="选择邮箱池中的可用邮箱"
            no-data-text="没有可用目标邮箱"
          >
            <el-option
              v-for="target in availableTargets"
              :key="target.id"
              :label="target.email"
              :value="target.id"
            />
          </el-select>
        </el-form-item>
      </el-form>

      <p class="email-change-hint">
        远端验证完成后才会提交本地账号；目标邮箱会在任务期间独立预留。
      </p>

      <el-alert
        v-if="run"
        :title="statusLabel(run.status)"
        :type="statusType(run.status)"
        :closable="false"
        show-icon
      >
        <div class="email-change-status">
          <span>阶段：{{ run.stage }}</span>
          <span v-if="run.errorCode">错误：{{ run.errorCode }}</span>
          <span v-if="run.errorMessage">{{ run.errorMessage }}</span>
        </div>
      </el-alert>
      <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" show-icon />
    </template>

    <template #footer>
      <el-button :disabled="active || submitting" @click="close">关闭</el-button>
      <el-button v-if="active" type="warning" :loading="submitting" @click="cancel">
        取消任务
      </el-button>
      <el-button
        v-else
        type="primary"
        :icon="Refresh"
        :loading="submitting"
        :disabled="!targetId || loadingTargets"
        @click="submit"
      >
        开始换绑
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.email-change-summary {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border-subtle);
}

.email-change-summary span,
.email-change-hint {
  color: var(--text-muted);
  font-size: 12px;
}

.email-change-summary strong {
  overflow: hidden;
  color: var(--text-primary);
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.email-change-target {
  width: 100%;
}

.email-change-hint {
  margin: 0 0 16px;
  line-height: 1.6;
}

.email-change-status {
  display: grid;
  gap: 4px;
  font-size: 12px;
  line-height: 1.5;
}
</style>
