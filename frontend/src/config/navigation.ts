export interface AppNavItem {
  id: string
  name: string
  path: string
}

export const appNavigation: AppNavItem[] = [
  { id: 'overview', name: '总览', path: '/overview' },
  { id: 'tasks', name: 'AI分析', path: '/tasks' },
  { id: 'chat', name: 'AI聊天', path: '/chat' },
  { id: 'memories', name: '记忆梦境', path: '/memories' },
  { id: 'schedule', name: '定时设置', path: '/schedule' },
  { id: 'settings', name: '功能设置', path: '/settings' },
]
