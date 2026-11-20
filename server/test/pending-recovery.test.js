const assert = require('node:assert/strict')
const { after, test } = require('node:test')
const {
  cleanupConversation,
  createConversation,
  request,
  setConversationSecurityStatus,
} = require('./helpers')

let conversationId

after(async () => {
  if (conversationId) {
    await cleanupConversation(conversationId)
  }
})

test('rechecks pending status with FastAPI and resumes only after it clears', async () => {
  const created = await createConversation()
  conversationId = created.conversationId
  assert.equal(created.result.status, 201)

  const aiSeed = await fetch('http://localhost:8000/chat', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ conversation_id: conversationId, prompt: 'hi' }),
  })
  assert.equal(aiSeed.status, 200)

  const savedState = await request(
    `/internal/conversation-state/${conversationId}`
  )
  assert.deepEqual(
    savedState.body.state.messages.map(({ role, content }) => ({ role, content })),
    [
      { role: 'user', content: 'hi' },
      { role: 'assistant', content: savedState.body.state.messages[1].content },
    ]
  )

  await setConversationSecurityStatus(conversationId, 'pending')
  const response = await request('/llm_response', {
    method: 'POST',
    body: { conversation_id: conversationId, prompt: 'What is 2 plus 2?' },
  })

  assert.equal(response.status, 200, JSON.stringify(response.body))
  assert.equal(response.body.security_status, 'clear')

  const history = await request(`/conversations/${conversationId}`)
  assert.equal(history.body.security_status, 'clear')
  assert.equal(history.body.messages.length, 2)
})

test('persists and restores the AI engine checkpoint through the internal endpoint', async () => {
  const created = await createConversation()
  try {
    const checkpoint = {
      messages: [
        { role: 'user', content: 'saved prompt' },
        { role: 'assistant', content: 'saved answer' },
      ],
      message_count: 2,
      checked_up_to: 0,
      security_status: 'clear',
    }

    const saved = await request(
      `/internal/conversation-state/${created.conversationId}`,
      { method: 'PUT', body: { state: checkpoint } }
    )
    assert.equal(saved.status, 200)

    const restored = await request(
      `/internal/conversation-state/${created.conversationId}`
    )
    assert.deepEqual(restored.body.state, checkpoint)

    const history = await request(`/conversations/${created.conversationId}`)
    assert.equal(history.body.security_status, 'clear')
  } finally {
    await cleanupConversation(created.conversationId)
  }
})

test('rejects browser-origin requests to the internal AI-state endpoint', async () => {
  const response = await request('/internal/conversation-state/forbidden-origin', {
    headers: { origin: 'https://untrusted.example' },
  })

  assert.equal(response.status, 403)
})

test('does not create missing conversations while loading internal AI state', async () => {
  const response = await request('/internal/conversation-state/not-created')

  assert.equal(response.status, 404)
})
