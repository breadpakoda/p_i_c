const assert = require('node:assert/strict')
const { spawn } = require('node:child_process')
const { createServer } = require('node:http')
const path = require('node:path')
const { after, test } = require('node:test')
const { MongoClient } = require('mongodb')
const { randomUUID } = require('node:crypto')

const mongoUri = process.env.MONGODB_URI || 'mongodb://localhost:27017'
const mongoDbName = process.env.MONGODB_DB || 'prompt_guard'
const nodePort = 5002
const reviewPort = 8001
const nodeUrl = `http://127.0.0.1:${nodePort}`
const reviewUrl = `http://127.0.0.1:${reviewPort}`
const conversationId = `legacy-${randomUUID()}`
let mongo
let nodeProcess
let reviewServer

async function waitForNode() {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (nodeProcess.exitCode !== null) {
      throw new Error(`Test Node server exited with code ${nodeProcess.exitCode}`)
    }

    try {
      const response = await fetch(`${nodeUrl}/health`)
      if (response.ok) return
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 100))
    }
  }
  throw new Error('Test Node server did not become ready')
}

after(async () => {
  if (nodeProcess && nodeProcess.exitCode === null) {
    const exited = new Promise((resolve) => nodeProcess.once('exit', resolve))
    nodeProcess.kill()
    await exited
  }
  if (reviewServer) {
    await new Promise((resolve, reject) => {
      reviewServer.close((error) => error ? reject(error) : resolve())
    })
  }
  if (mongo) {
    await mongo.db(mongoDbName).collection('conversations')
      .deleteOne({ conversation_id: conversationId })
    await mongo.close()
  }
})

test('rechecks a legacy blocked transcript but leaves current blocks untouched', async () => {
  reviewServer = createServer(async (req, res) => {
    let body = ''
    for await (const chunk of req) body += chunk
    assert.equal(req.url, '/layer3-review')
    assert.ok(JSON.parse(body).messages.length > 0)
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ is_danger: false }))
  })
  await new Promise((resolve) => reviewServer.listen(reviewPort, '127.0.0.1', resolve))

  mongo = new MongoClient(mongoUri)
  await mongo.connect()
  await mongo.db(mongoDbName).collection('conversations').insertOne({
    conversation_id: conversationId,
    messages: [
      { role: 'user', content: 'Explain photosynthesis.' },
      { role: 'assistant', content: 'Plants use light to make sugars.' },
    ],
    security_status: 'blocked',
    created_at: new Date(),
    updated_at: new Date(),
  })

  nodeProcess = spawn(process.execPath, ['index.js'], {
    cwd: path.resolve(__dirname, '..'),
    env: {
      ...process.env,
      PORT: String(nodePort),
      AI_ENGINE_URL: reviewUrl,
    },
    stdio: 'ignore',
  })
  await waitForNode()

  const response = await fetch(`${nodeUrl}/conversations/${conversationId}/recheck`, {
    method: 'POST',
  })
  assert.equal(response.status, 200)
  assert.equal((await response.json()).security_status, 'clear')

  const saved = await mongo.db(mongoDbName).collection('conversations')
    .findOne({ conversation_id: conversationId })
  assert.equal(saved.security_status, 'clear')
  assert.equal(saved.engine_state.security_status, 'clear')
})
