import assert from 'node:assert/strict'
import test from 'node:test'

import { useSkillManager } from '../src/composables/useSkillManager.ts'
import type { SkillInfo, SkillListItem } from '../src/types.ts'

function createSkillListItem(overrides: Partial<SkillListItem> = {}): SkillListItem {
  return {
    id: 'builtin_utils',
    name: 'builtin_utils',
    description: 'desc',
    source: 'builtin',
    enabled: true,
    layer: 'runtime',
    can_toggle: false,
    can_delete: false,
    policy_label: '运行时底座',
    policy_summary: '始终启用，不允许停用或删除。',
    ...overrides,
  }
}

function createSkill(overrides: Partial<SkillInfo> = {}): SkillInfo {
  return {
    ...createSkillListItem(),
    location: 'C:\\skills\\builtin_utils',
    has_handler: true,
    tool_names: ['http_get'],
    run_types: ['analysis', 'chat'],
    category: 'utility',
    compatibility_level: 'native',
    compatibility_summary: 'ok',
    issues: [],
    support_files: [],
    clawhub_slug: null,
    clawhub_version: null,
    clawhub_url: null,
    published_at: null,
    ...overrides,
  }
}

test('loadSkills populates skill list and counters', async () => {
  const manager = useSkillManager({
    listSkills: async () => [
      createSkillListItem(),
      createSkillListItem({
        id: 'prompt-skill',
        source: 'workspace',
        layer: 'standard',
        enabled: false,
        can_toggle: true,
        can_delete: true,
        policy_label: '工作区技能',
        policy_summary: '支持启停管理，也允许从工作区删除。',
      }),
    ],
    importSkillHubSkill: async () => createSkill(),
    importSkillArchive: async () => createSkill(),
    reloadSkills: async () => [],
    enableSkill: async () => createSkill(),
    disableSkill: async () => createSkill({ enabled: false }),
    deleteSkill: async () => undefined,
  })

  await manager.loadSkills()

  assert.equal(manager.skills.value.length, 2)
  assert.equal(manager.enabledCount.value, 1)
  assert.equal(manager.standardCount.value, 1)
  assert.deepEqual(manager.installedOverview.value, {
    total: 2,
    runtime: 1,
    standard: 1,
  })
  assert.deepEqual(manager.enabledOverview.value, {
    total: 1,
    runtime: 1,
    standard: 0,
  })
  assert.equal(manager.runtimeSkills.value.length, 1)
  assert.equal(manager.standardSkills.value.length, 1)
})

test('importFromSkillHub trims input and keeps imported skill disabled', async () => {
  const imported = createSkill({
    id: 'newsnow-v2',
    name: 'NewsNow V2',
    source: 'workspace',
    layer: 'standard',
    enabled: false,
    has_handler: false,
    compatibility_level: 'needs_attention',
    clawhub_slug: 'newsnow-v2',
    can_toggle: true,
    can_delete: true,
    policy_label: '工作区技能',
    policy_summary: '支持启停管理，也允许从工作区删除。',
  })
  let capturedInput = ''

  const manager = useSkillManager({
    listSkills: async () => [],
    importSkillHubSkill: async (value: string) => {
      capturedInput = value
      return imported
    },
    importSkillArchive: async () => imported,
    reloadSkills: async () => [],
    enableSkill: async () => imported,
    disableSkill: async () => imported,
    deleteSkill: async () => undefined,
  })

  manager.importInput.value = '  https://skillhub.cn/skills/newsnow-v2  '
  await manager.importFromSkillHub()

  assert.equal(capturedInput, 'https://skillhub.cn/skills/newsnow-v2')
  assert.equal(manager.importInput.value, '')
  assert.equal(manager.skills.value[0]?.id, 'newsnow-v2')
})

test('importSkill prefers selected zip archive over SkillHub input', async () => {
  const imported = createSkill({
    id: 'uploaded-skill',
    name: 'Uploaded Skill',
    source: 'workspace',
    layer: 'standard',
    enabled: false,
    can_toggle: true,
    can_delete: true,
    policy_label: '工作区技能',
    policy_summary: '支持启停管理，也允许从工作区删除。',
  })
  const file = new File(['demo'], 'uploaded-skill.zip', { type: 'application/zip' })
  let archiveCalls = 0
  let skillHubCalls = 0

  const manager = useSkillManager({
    listSkills: async () => [],
    importSkillHubSkill: async () => {
      skillHubCalls += 1
      return imported
    },
    importSkillArchive: async (archive: File) => {
      archiveCalls += 1
      assert.equal(archive.name, 'uploaded-skill.zip')
      return imported
    },
    reloadSkills: async () => [],
    enableSkill: async () => imported,
    disableSkill: async () => imported,
    deleteSkill: async () => undefined,
  })

  manager.importInput.value = 'https://skillhub.cn/skills/should-not-run'
  manager.setImportFile(file)
  await manager.importSkill()

  assert.equal(archiveCalls, 1)
  assert.equal(skillHubCalls, 0)
  assert.equal(manager.selectedArchive.value, null)
  assert.equal(manager.importInput.value, '')
  assert.equal(manager.skills.value[0]?.id, 'uploaded-skill')
})

test('toggleSkill switches between enable and disable requests', async () => {
  let enabledCalls = 0
  let disabledCalls = 0
  const manager = useSkillManager({
    listSkills: async () => [],
    importSkillHubSkill: async () => createSkill(),
    importSkillArchive: async () => createSkill(),
    reloadSkills: async () => [],
    enableSkill: async (skillId: string) => {
      enabledCalls += 1
      return createSkill({ id: skillId, enabled: true })
    },
    disableSkill: async (skillId: string) => {
      disabledCalls += 1
      return createSkill({ id: skillId, enabled: false })
    },
    deleteSkill: async () => undefined,
  })

  manager.skills.value = [createSkill({ id: 'demo-skill', enabled: false })]
  await manager.toggleSkill(manager.skills.value[0])
  assert.equal(enabledCalls, 1)
  assert.equal(manager.skills.value[0].enabled, true)

  await manager.toggleSkill(manager.skills.value[0])
  assert.equal(disabledCalls, 1)
  assert.equal(manager.skills.value[0].enabled, false)
})

test('toggleSkill keeps same-source skills in name order after disabling', async () => {
  const manager = useSkillManager({
    listSkills: async () => [],
    importSkillHubSkill: async () => createSkill(),
    importSkillArchive: async () => createSkill(),
    reloadSkills: async () => [],
    enableSkill: async (skillId: string) => createSkill({
      id: skillId,
      name: 'Alpha Skill',
      source: 'workspace',
      layer: 'standard',
      enabled: true,
      can_toggle: true,
      can_delete: true,
      policy_label: '工作区技能',
      policy_summary: '支持启停管理，也允许从工作区删除。',
    }),
    disableSkill: async (skillId: string) => createSkill({
      id: skillId,
      name: 'Alpha Skill',
      source: 'workspace',
      layer: 'standard',
      enabled: false,
      can_toggle: true,
      can_delete: true,
      policy_label: '工作区技能',
      policy_summary: '支持启停管理，也允许从工作区删除。',
    }),
    deleteSkill: async () => undefined,
  })

  manager.skills.value = [
    createSkill({ id: 'alpha-skill', name: 'Alpha Skill', source: 'workspace', layer: 'standard', enabled: true, can_toggle: true, can_delete: true, policy_label: '工作区技能', policy_summary: '支持启停管理，也允许从工作区删除。' }),
    createSkill({ id: 'beta-skill', name: 'Beta Skill', source: 'workspace', layer: 'standard', enabled: true, can_toggle: true, can_delete: true, policy_label: '工作区技能', policy_summary: '支持启停管理，也允许从工作区删除。' }),
  ]

  await manager.toggleSkill(manager.skills.value[0])

  assert.deepEqual(
    manager.skills.value.map((item) => item.id),
    ['alpha-skill', 'beta-skill'],
  )
  assert.equal(manager.skills.value[0].enabled, false)
})

test('deleteSkill removes workspace skill', async () => {
  let deleteCalls = 0
  const manager = useSkillManager({
    listSkills: async () => [],
    importSkillHubSkill: async () => createSkill(),
    importSkillArchive: async () => createSkill(),
    reloadSkills: async () => [],
    enableSkill: async () => createSkill(),
    disableSkill: async () => createSkill({ enabled: false }),
    deleteSkill: async (skillId: string) => {
      deleteCalls += 1
      assert.equal(skillId, 'uploaded-skill')
    },
  })

  manager.skills.value = [
    createSkill(),
    createSkill({ id: 'uploaded-skill', name: 'Uploaded Skill', source: 'workspace', layer: 'standard', enabled: false, can_toggle: true, can_delete: true, policy_label: '工作区技能', policy_summary: '支持启停管理，也允许从工作区删除。' }),
  ]

  await manager.deleteSkill(manager.skills.value[1])

  assert.equal(deleteCalls, 1)
  assert.equal(manager.skills.value.some((item) => item.id === 'uploaded-skill'), false)
})

test('runtime skills stay ahead of standard skills after merge sorting', async () => {
  const manager = useSkillManager({
    listSkills: async () => [],
    importSkillHubSkill: async () => createSkill(),
    importSkillArchive: async () => createSkill(),
    reloadSkills: async () => [],
    enableSkill: async (skillId: string) => createSkill({
      id: skillId,
      name: 'Alpha Standard',
      source: 'workspace',
      layer: 'standard',
      enabled: true,
      can_toggle: true,
      can_delete: true,
      policy_label: '工作区技能',
      policy_summary: '支持启停管理，也允许从工作区删除。',
    }),
    disableSkill: async () => createSkill({ enabled: false }),
    deleteSkill: async () => undefined,
  })

  manager.skills.value = [
    createSkillListItem({ id: 'builtin-utils', name: 'Runtime Core', layer: 'runtime', source: 'builtin', enabled: true }),
    createSkillListItem({ id: 'alpha-standard', name: 'Alpha Standard', layer: 'standard', source: 'workspace', enabled: false, can_toggle: true, can_delete: true, policy_label: '工作区技能', policy_summary: '支持启停管理，也允许从工作区删除。' }),
  ]

  await manager.toggleSkill(manager.skills.value[1])

  assert.deepEqual(
    manager.skills.value.map((item) => item.layer),
    ['runtime', 'standard'],
  )
})
