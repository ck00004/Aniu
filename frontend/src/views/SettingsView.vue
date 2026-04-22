<template>
  <div class="tab-content">
    <section class="content-grid content-grid-primary">
      <section class="panel settings-panel">
        <div class="panel-head">
          <div class="head-main">
            <h2>功能设置</h2>
            <p class="section-kicker">Configuration</p>
          </div>
        </div>

        <div class="settings-two-col">
          <div class="settings-left">
            <label class="field">
              <span>Base URL</span>
              <input v-model="settings.llm_base_url" placeholder="https://api.openai.com/v1" />
              <p class="field-help">大模型 API 的基础地址，默认可填写 OpenAI 兼容地址。</p>
            </label>
            <label class="field">
              <span>API Key</span>
              <input v-model="settings.llm_api_key" type="password" placeholder="sk-..." />
              <p class="field-help">用于访问大模型 API 的密钥。</p>
            </label>
            <label class="field">
              <span>模型名</span>
              <input v-model="settings.llm_model" />
              <p class="field-help">要使用的大模型名称，例如 `gpt-4o-mini`。</p>
            </label>
            <label class="field">
              <span>妙想密钥</span>
              <input v-model="settings.mx_api_key" type="password" placeholder="妙想接口 apikey" />
              <p class="field-help">用于访问东方财富妙想接口的密钥。</p>
            </label>
            <label class="field">
              <span>JIN10 API Base URL</span>
              <input
                v-model="settings.jin10_api_base_url"
                placeholder="http://127.0.0.1:3000"
              />
              <p class="field-help">
                用于连接 Jin10 新闻服务，分析前会拉取当天新闻作为辅助参考。
              </p>
            </label>
          </div>
          <div class="settings-right">
            <label class="field">
              <span>系统提示词</span>
              <textarea v-model="settings.system_prompt" rows="8" />
              <p class="field-help">指导大模型行为的系统提示词，会影响 AI 的分析和决策方式。</p>
            </label>
          </div>
        </div>

        <div v-if="errorMessage" class="error-banner">{{ errorMessage }}</div>

        <div class="panel-actions">
          <button
            class="button primary"
            :class="{ 'is-loading': busy }"
            :disabled="busy"
            @click="saveSettings"
          >
            保存设置
          </button>
        </div>
      </section>

      <section class="panel skills-panel">
        <div class="panel-head">
          <div class="head-main">
            <h2>技能管理</h2>
            <p class="section-kicker">Skills</p>
          </div>
          <button
            class="button ghost small soft-header-button overview-refresh-button"
            :class="{ 'is-loading': skillsBusy }"
            :disabled="skillsBusy"
            @click="reloadSkills"
          >
            重新扫描
          </button>
        </div>

        <div class="skills-toolbar">
          <div class="skills-overview-card">
            <span class="meta-label">已安装技能</span>
            <strong>总数 {{ installedOverview.total }}</strong>
            <div class="skills-overview-breakdown">
              <span>系统技能 {{ installedOverview.builtin }}</span>
              <span>用户技能 {{ installedOverview.workspace }}</span>
            </div>
          </div>
          <div class="skills-overview-card">
            <span class="meta-label">已启用技能</span>
            <strong>总数 {{ enabledOverview.total }}</strong>
            <div class="skills-overview-breakdown">
              <span>系统技能 {{ enabledOverview.builtin }}</span>
              <span>用户技能 {{ enabledOverview.workspace }}</span>
            </div>
          </div>
          <div class="skills-import-cluster">
            <span class="meta-label skill-import-hint">输入 SkillHub 链接或添加本地 zip 技能包</span>
            <div class="skills-import-inline">
              <label class="field skill-import-field">
                <div class="skill-import-control" :class="{ 'is-disabled': skillsBusy }">
                  <input
                    v-model="importInput"
                    placeholder="https://skillhub.cn链接或者技能名称"
                    :disabled="skillsBusy"
                    @input="handleImportInput"
                  />
                  <button
                    type="button"
                    class="button ghost small skill-import-file-button"
                    :disabled="skillsBusy"
                    @click="openImportFileDialog"
                  >
                    {{ selectedArchive ? '更换文件' : '添加文件' }}
                  </button>
                </div>
                <input
                  ref="skillArchiveInputRef"
                  class="skill-import-native-input"
                  type="file"
                  accept=".zip,application/zip"
                  :disabled="skillsBusy"
                  @change="handleImportFileChange"
                />
              </label>
              <button
                class="button primary skills-import-submit"
                :class="{ 'is-loading': skillsBusy }"
                :disabled="skillsBusy"
                @click="importSkill"
              >
                导入技能
              </button>
            </div>
            <p v-if="selectedArchive" class="skill-import-selected">
              已选择文件：{{ selectedArchive.name }}
            </p>
          </div>
        </div>

        <div v-if="skillsErrorMessage" class="error-banner">{{ skillsErrorMessage }}</div>

        <div v-if="skills.length" class="skill-card-list">
          <article v-for="skill in skills" :key="skill.id" class="skill-card">
            <div class="skill-card-copy">
              <div class="skill-title-row">
                <strong>{{ skill.name }}</strong>
                <span
                  class="skill-source-badge"
                  :class="skill.source === 'builtin' ? 'is-system' : 'is-user'"
                >
                  {{ skill.source === 'builtin' ? '系统技能' : '用户技能' }}
                </span>
              </div>

              <div class="skill-info-stack">
                <div class="skill-info-block skill-info-description-block">
                  <span class="meta-label">技能介绍</span>
                  <p class="skill-card-description">
                    {{ skill.description || '暂无技能描述。' }}
                  </p>
                </div>
              </div>
            </div>

            <div class="skill-card-footer">
              <button
                v-if="skill.source === 'workspace'"
                type="button"
                class="button ghost small soft-header-button skill-delete-action"
                :disabled="skillsBusy"
                @click="deleteSkill(skill)"
              >
                删除
              </button>
              <button
                v-else
                type="button"
                class="button ghost small soft-header-button skill-delete-action is-placeholder"
                disabled
              >
                不可删除
              </button>
              <button
                type="button"
                class="skill-toggle"
                :class="{ 'is-on': skill.enabled }"
                :disabled="skillsBusy || !canToggleSkill(skill)"
                role="switch"
                :aria-checked="skill.enabled"
                @click="toggleSkill(skill)"
              >
                <span class="skill-toggle-thumb" aria-hidden="true"></span>
                {{ skill.enabled ? '启用' : '停用' }}
              </button>
            </div>
          </article>
        </div>

        <div v-else class="empty-state">
          <p>当前还没有可展示的技能。</p>
        </div>
      </section>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'

import { useSkillManager } from '@/composables/useSkillManager'
import { useAppStore } from '@/stores/legacy'
import type { SkillListItem } from '@/types'

const store = useAppStore()
const { settings, busy, errorMessage } = storeToRefs(store)
const { saveSettings } = store
const {
  skills,
  importInput,
  selectedArchive,
  busy: skillsBusy,
  errorMessage: skillsErrorMessage,
  installedOverview,
  enabledOverview,
  loadSkills,
  setImportFile,
  importSkill: submitSkillImport,
  reloadSkills: reloadSkillList,
  toggleSkill: toggleManagedSkill,
  deleteSkill: deleteManagedSkill,
} = useSkillManager()
const skillArchiveInputRef = ref<HTMLInputElement | null>(null)

function openImportFileDialog() {
  if (skillArchiveInputRef.value) {
    skillArchiveInputRef.value.value = ''
    skillArchiveInputRef.value.click()
  }
}

function resetNativeSkillInput() {
  if (skillArchiveInputRef.value) {
    skillArchiveInputRef.value.value = ''
  }
}

function handleImportInput() {
  if (!importInput.value.trim()) {
    return
  }
  setImportFile(null)
  resetNativeSkillInput()
}

function handleImportFileChange(event: Event) {
  const input = event.target as HTMLInputElement | null
  const file = input?.files?.[0] ?? null
  setImportFile(file)
}

async function importSkill() {
  const imported = await submitSkillImport()
  if (imported) {
    resetNativeSkillInput()
  }
}

async function reloadSkills() {
  await reloadSkillList()
}

async function toggleSkill(skill: SkillListItem) {
  if (!canToggleSkill(skill)) {
    return
  }
  await toggleManagedSkill(skill)
}

async function deleteSkill(skill: SkillListItem) {
  if (skill.source !== 'workspace') {
    return
  }
  await deleteManagedSkill(skill)
}

function canToggleSkill(skill: SkillListItem) {
  return skill.id !== 'builtin_utils'
}

onMounted(async () => {
  try {
    await Promise.all([
      store.loadSettings(),
      loadSkills(),
    ])
  } catch (error) {
    errorMessage.value = (error as Error).message
  }
})
</script>
