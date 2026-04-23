import { computed, ref } from 'vue'

import { api } from '../services/api.ts'
import type { SkillListItem } from '../types.ts'

type SkillApiClient = Pick<
  typeof api,
  | 'listSkills'
  | 'importSkillHubSkill'
  | 'importSkillArchive'
  | 'reloadSkills'
  | 'enableSkill'
  | 'disableSkill'
  | 'deleteSkill'
>

function mergeSkill(skills: SkillListItem[], nextSkill: SkillListItem): SkillListItem[] {
  const filtered = skills.filter((item) => item.id !== nextSkill.id)
  return sortSkills([...filtered, nextSkill])
}

function sortSkills(skills: SkillListItem[]): SkillListItem[] {
  return [...skills].sort((left, right) => {
    if (left.layer !== right.layer) {
      return left.layer === 'runtime' ? -1 : 1
    }
    if (left.source !== right.source) {
      return left.source === 'builtin' ? -1 : 1
    }
    return left.name.localeCompare(right.name, 'zh-CN')
  })
}

export function useSkillManager(client: SkillApiClient = api) {
  const skills = ref<SkillListItem[]>([])
  const importInput = ref('')
  const selectedArchive = ref<File | null>(null)
  const busy = ref(false)
  const errorMessage = ref('')

  const runtimeSkills = computed(() => skills.value.filter((item) => item.layer === 'runtime'))
  const standardSkills = computed(() => skills.value.filter((item) => item.layer === 'standard'))
  const runtimeCount = computed(() => runtimeSkills.value.length)
  const standardCount = computed(() => standardSkills.value.length)
  const enabledCount = computed(() => skills.value.filter((item) => item.enabled).length)
  const enabledRuntimeCount = computed(() => (
    runtimeSkills.value.filter((item) => item.enabled).length
  ))
  const enabledStandardCount = computed(() => (
    standardSkills.value.filter((item) => item.enabled).length
  ))
  const installedOverview = computed(() => ({
    total: skills.value.length,
    runtime: runtimeCount.value,
    standard: standardCount.value,
  }))
  const enabledOverview = computed(() => ({
    total: enabledCount.value,
    runtime: enabledRuntimeCount.value,
    standard: enabledStandardCount.value,
  }))

  async function loadSkills() {
    busy.value = true
    errorMessage.value = ''
    try {
      skills.value = await client.listSkills()
      skills.value = sortSkills(skills.value)
      return skills.value
    } catch (error) {
      errorMessage.value = (error as Error).message
      throw error
    } finally {
      busy.value = false
    }
  }

  async function importFromSkillHub() {
    const normalized = importInput.value.trim()
    if (!normalized) {
      errorMessage.value = '请输入 SkillHub 技能链接或 slug。'
      return null
    }

    busy.value = true
    errorMessage.value = ''
    try {
      const imported = await client.importSkillHubSkill(normalized)
      skills.value = mergeSkill(skills.value, imported)
      importInput.value = ''
      return imported
    } catch (error) {
      errorMessage.value = (error as Error).message
      throw error
    } finally {
      busy.value = false
    }
  }

  function setImportFile(file: File | null) {
    selectedArchive.value = file
    if (file) {
      importInput.value = ''
    }
  }

  async function importFromZip() {
    if (!selectedArchive.value) {
      errorMessage.value = '请先选择一个 zip 技能包。'
      return null
    }

    busy.value = true
    errorMessage.value = ''
    try {
      const imported = await client.importSkillArchive(selectedArchive.value)
      skills.value = mergeSkill(skills.value, imported)
      selectedArchive.value = null
      importInput.value = ''
      return imported
    } catch (error) {
      errorMessage.value = (error as Error).message
      throw error
    } finally {
      busy.value = false
    }
  }

  async function importSkill() {
    if (selectedArchive.value) {
      return importFromZip()
    }
    if (importInput.value.trim()) {
      return importFromSkillHub()
    }
    errorMessage.value = '请输入 SkillHub 链接 / slug，或选择一个 zip 技能包。'
    return null
  }

  async function reloadSkills() {
    busy.value = true
    errorMessage.value = ''
    try {
      skills.value = await client.reloadSkills()
      skills.value = sortSkills(skills.value)
      return skills.value
    } catch (error) {
      errorMessage.value = (error as Error).message
      throw error
    } finally {
      busy.value = false
    }
  }

  async function toggleSkill(skill: SkillListItem) {
    busy.value = true
    errorMessage.value = ''
    try {
      const updated = skill.enabled
        ? await client.disableSkill(skill.id)
        : await client.enableSkill(skill.id)
      skills.value = mergeSkill(skills.value, updated)
      return updated
    } catch (error) {
      errorMessage.value = (error as Error).message
      throw error
    } finally {
      busy.value = false
    }
  }

  async function deleteSkill(skill: SkillListItem) {
    busy.value = true
    errorMessage.value = ''
    try {
      await client.deleteSkill(skill.id)
      skills.value = skills.value.filter((item) => item.id !== skill.id)
    } catch (error) {
      errorMessage.value = (error as Error).message
      throw error
    } finally {
      busy.value = false
    }
  }

  return {
    skills,
    importInput,
    selectedArchive,
    busy,
    errorMessage,
    runtimeSkills,
    standardSkills,
    runtimeCount,
    standardCount,
    enabledCount,
    installedOverview,
    enabledOverview,
    loadSkills,
    setImportFile,
    importFromSkillHub,
    importFromZip,
    importSkill,
    reloadSkills,
    toggleSkill,
    deleteSkill,
  }
}
