export interface ScrollMetrics {
  scrollHeight: number
  scrollTop: number
  clientHeight: number
}

export const CHAT_AUTO_FOLLOW_THRESHOLD_PX = 28

export function isChatScrollNearBottom(
  metrics: ScrollMetrics,
  threshold = CHAT_AUTO_FOLLOW_THRESHOLD_PX,
): boolean {
  return metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight <= threshold
}

export function shouldAutoFollowChatScroll(options: {
  sending: boolean
  messageCount: number
  followScroll: boolean
}): boolean {
  return options.sending && options.messageCount > 0 && options.followScroll
}
