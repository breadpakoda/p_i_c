const assert = require('node:assert/strict')
const path = require('node:path')
const { spawn } = require('node:child_process')
const { after, test } = require('node:test')
const {
  cleanupConversation,
  createConversation,
} = require('./helpers')

const secondServerUrl = 'http://localhost:5001'
let secondServer
let conversationId

async function waitForSecondServer() {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (secondServer.exitCode !== null) {
      throw new Error(`Secondary server exited with code ${secondServer.exitCode}`)
    }

    try {
      const response = await fetch(`${secondServerUrl}/health`)
      if (response.ok) return
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 100))
    }
  }
  throw new Error('Secondary Node server did not become ready')
}

async function sendPrompt(baseUrl, prompt) {
  const response = await fetch(`${baseUrl}/llm_response`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ conversation_id: conversationId, prompt }),
  })
  return { status: response.status, body: await response.json() }
}

after(async () => {
  if (conversationId) await cleanupConversation(conversationId)
  if (secondServer && secondServer.exitCode === null) {
    const exited = new Promise((resolve) => secondServer.once('exit', resolve))
    secondServer.kill()
    await exited
  }
})

test('serializes a conversation across two Node server processes', async () => {
  secondServer = spawn(process.execPath, ['index.js'], {
    cwd: path.resolve(__dirname, '..'),
    env: { ...process.env, PORT: '5001' },
    stdio: 'ignore',
  })
  await waitForSecondServer()

  const created = await createConversation()
  conversationId = created.conversationId
  const firstPrompt = 'What is 2 plus 2?'
  const secondPrompt = 'Name one planet in our solar system.'

  const firstRequest = sendPrompt('http://localhost:5000', firstPrompt)
  await new Promise((resolve) => setTimeout(resolve, 100))
  const secondRequest = sendPrompt(secondServerUrl, secondPrompt)
  const responses = await Promise.all([firstRequest, secondRequest])

  assert.ok(responses.every((response) => response.status === 200))
  const historyResponse = await fetch(
    `http://localhost:5000/conversations/${conversationId}`
  )
  const history = await historyResponse.json()
  assert.deepEqual(
    history.messages.map((message) => message.role),
    ['user', 'assistant', 'user', 'assistant']
  )
  assert.deepEqual(
    history.messages.filter((message) => message.role === 'user')
      .map((message) => message.content)
      .sort(),
    [firstPrompt, secondPrompt].sort()
  )
})
