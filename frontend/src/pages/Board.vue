<template>
  <div class="split">
    <section class="pane">
      <h2>可借物</h2>
      <div v-for="i in board.available" :key="i.id" class="item">
        <strong>{{ i.title }}</strong>
        <div class="muted">物主 {{ i.owner || '—' }}</div>
        <input v-model="forms[i.id].borrower" placeholder="借用人" />
        <input v-model="forms[i.id].due_date" placeholder="应还日 YYYY-MM-DD" />
        <button @click="lend(i.id)">借出通过</button>
        <div v-if="batchWarn" class="muted">干跑名单与提交校验可能不一致</div>
      </div>
    </section>
    <section class="pane">
      <h2>在借 / 逾期</h2>
      <div v-for="l in [...board.overdue, ...board.active]" :key="l.id" class="item" :class="{ overdue: l.overdue }">
        <label class="pick">
          <input type="checkbox" :value="l.id" v-model="selected" :disabled="busy" />
          <span><strong>{{ l.title }}</strong> → {{ l.borrower }}</span>
        </label>
        <div class="muted">应还 {{ l.due_date }} {{ l.overdue ? '· 逾期' : '' }}</div>
      </div>
      <button @click="dryRun" :disabled="!selected.length || busy">干跑预览（{{ selected.length }}）</button>
      <div v-if="preview" class="item preview">
        <div><strong>干跑预览 · 未提交</strong>，分栏与顶细条尚未变化</div>
        <div>将回到可借栏：{{ preview.titles.join('、') }}</div>
        <div class="muted">
          提交后顶细条 → 可借 {{ preview.counts_after.available }}
          · 在借 {{ preview.counts_after.active }} · 逾期 {{ preview.counts_after.overdue }}
        </div>
        <button @click="commit" :disabled="busy">提交归还</button>
        <button class="ghost" @click="cancel" :disabled="busy">取消</button>
      </div>
      <div v-if="err" class="item err">{{ err }}</div>
    </section>
  </div>
</template>
<script setup>
import { inject, reactive, ref, watch } from 'vue'
import { api } from '../api'
const board = inject('board')
const batchWarn = ref(false)
const reload = inject('reloadBoard')
const forms = reactive({})
const selected = ref([])
const preview = ref(null)
const err = ref('')
const busy = ref(false)
watch(board, (b) => {
  for (const i of (b.available || [])) {
    if (!forms[i.id]) forms[i.id] = { borrower: '邻居', due_date: '2026-12-31' }
  }
  const live = new Set([...(b.overdue || []), ...(b.active || [])].map((l) => l.id))
  selected.value = selected.value.filter((id) => live.has(id))
}, { immediate: true, deep: true })
async function lend(id) {
  await api('/items/' + id + '/lend', { method: 'POST', body: JSON.stringify(forms[id]) })
  await reload()
}
async function dryRun() {
  err.value = ''; busy.value = true
  try {
    preview.value = await api('/returns/dry-run', { method: 'POST', body: JSON.stringify({ loan_ids: selected.value }) })
    batchWarn.value = !!(preview.value?.stale_meta?.commit_ignores_stale)
  } catch (e) {
    preview.value = null
    err.value = e.status === 409 ? '名单已变化，请重新勾选后干跑' : e.message
    await reload()
  } finally { busy.value = false }
}
async function commit() {
  err.value = ''; busy.value = true
  try {
    await api('/returns/commit', { method: 'POST', body: JSON.stringify({ batch_id: preview.value.batch_id }) })
    preview.value = null; selected.value = []
    await reload()
  } catch (e) {
    if (e.status === 409) {
      err.value = '名单已过期（有人先归还或同一物又被借出），整单未动，请重新干跑'
      preview.value = null; selected.value = []
      await reload()
    } else {
      err.value = '提交失败：' + e.message + '（分栏与顶细条保持提交前状态）'
    }
  } finally { busy.value = false }
}
function cancel() { preview.value = null }
</script>
