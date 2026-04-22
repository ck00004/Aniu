import assert from 'node:assert/strict'
import test from 'node:test'

import {
  CHAT_AUTO_FOLLOW_THRESHOLD_PX,
  isChatScrollNearBottom,
  shouldAutoFollowChatScroll,
} from '../src/utils/chatScrollFollow.ts'

test('isChatScrollNearBottom only follows when the user stays within the bottom threshold', () => {
  assert.equal(
    isChatScrollNearBottom({
      scrollHeight: 1000,
      scrollTop: 472,
      clientHeight: 500,
    }),
    true,
  )

  assert.equal(
    isChatScrollNearBottom({
      scrollHeight: 1000,
      scrollTop: 471,
      clientHeight: 500,
    }),
    false,
  )

  assert.equal(CHAT_AUTO_FOLLOW_THRESHOLD_PX, 28)
})

test('shouldAutoFollowChatScroll stops following once the user scrolls away during streaming', () => {
  assert.equal(
    shouldAutoFollowChatScroll({
      sending: true,
      messageCount: 2,
      followScroll: true,
    }),
    true,
  )

  assert.equal(
    shouldAutoFollowChatScroll({
      sending: true,
      messageCount: 2,
      followScroll: false,
    }),
    false,
  )

  assert.equal(
    shouldAutoFollowChatScroll({
      sending: false,
      messageCount: 2,
      followScroll: true,
    }),
    false,
  )
})
