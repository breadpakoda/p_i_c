const path = require('path')
const cors = require('cors')
const express = require('express')
const axios = require('axios')
const { MongoClient } = require('mongodb')
const { randomUUID } = require('crypto')
require('dotenv').config({ path: path.resolve(__dirname, '../.env') })

const MONGODB_URI = process.env.MONGODB_URI || 'mongodb://localhost:27017'
const MONGODB_DB = process.env.MONGODB_DB || 'prompt_guard'
const FASTAPI_BASE_URL = process.env.AI_ENGINE_URL || 'http://localhost:8000'
const INTERNAL_SERVICE_TOKEN = process.env.INTERNAL_SERVICE_TOKEN || ''
const CORS_ORIGIN = process.env.CORS_ORIGIN || '*'

const app = express()
app.use(cors(CORS_ORIGIN === '*' ? {} : { origin: CORS_ORIGIN }))
app.use(express.json({ limit: '2mb' }))

let mongoClient = null
let conversationCollection = null
let conversationLockCollection = null
const CONVERSATION_LOCK_LEASE_MS = 5 * 60 * 1000
const CONVERSATION_LOCK_RENEW_MS = 30 * 1000
const CONVERSATION_LOCK_WAIT_MS = 60 * 1000

async function getConversationCollection() {
  if (conversationCollection) return conversationCollection

  mongoClient = new MongoClient(MONGODB_URI)
  await mongoClient.connect()

  const db = mongoClient.db(MONGODB_DB)
  conversationCollection = db.collection('conversations')
  conversationLockCollection = db.collection('conversation_locks')

  await conversationCollection.createIndex({ conversation_id: 1 }, { unique: true })
  await conversationCollection.createIndex({ updated_at: -1 })
  await conversationLockCollection.createIndex(
    { expires_at: 1 },
    { expireAfterSeconds: 0 }
  )

  return conversationCollection
}

function buildConversationSummary(doc) {
  const messages = Array.isArray(doc.messages) ? doc.messages : []
  const previewMessage = messages
    .slice()
    .reverse()
    .find((message) => message && message.content && String(message.content).trim())

  return {
    conversation_id: doc.conversation_id,
    security_status: doc.security_status || 'clear',
    block_source: doc.block_source,
    can_recheck: doc.security_status === 'blocked' && !doc.block_source,
    created_at: doc.created_at,
    updated_at: doc.updated_at,
    title: previewMessage
      ? (previewMessage.role === 'user' ? 'User prompt' : 'Assistant reply')
      : 'New chat',
    preview: previewMessage ? previewMessage.content : 'No messages yet',
    messages,
  }
}

function isLocalRequest(req) {
  return ['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(req.ip)
}

function isInternalRequest(req) {
  const loopback = isLocalRequest(req) && !req.get('origin')
  const authenticated =
    INTERNAL_SERVICE_TOKEN &&
    req.get('x-internal-token') === INTERNAL_SERVICE_TOKEN
  return loopback || authenticated
}

function getDefaultEngineState(conversation) {
  const messages = Array.isArray(conversation.engine_state?.messages)
    ? conversation.engine_state.messages
    : (Array.isArray(conversation.messages) ? conversation.messages : [])

  return {
    messages,
    message_count: Number.isInteger(conversation.engine_state?.message_count)
      ? conversation.engine_state.message_count
      : messages.length,
    checked_up_to: Number.isInteger(conversation.engine_state?.checked_up_to)
      ? conversation.engine_state.checked_up_to
      : 0,
    security_status:
      conversation.security_status === 'blocked' ||
      conversation.engine_state?.security_status === 'blocked'
        ? 'blocked'
        : conversation.security_status === 'pending' ||
            conversation.engine_state?.security_status === 'pending'
          ? 'pending'
          : 'clear',
  }
}

async function acquireConversationLock(conversationId) {
  await getConversationCollection()
  const owner = randomUUID()
  const deadline = Date.now() + CONVERSATION_LOCK_WAIT_MS

  while (Date.now() < deadline) {
    const now = new Date()
    try {
      const result = await conversationLockCollection.findOneAndUpdate(
        {
          _id: conversationId,
          $or: [
            { expires_at: { $lte: now } },
            { expires_at: { $exists: false } },
          ],
        },
        {
          $set: {
            owner,
            expires_at: new Date(now.getTime() + CONVERSATION_LOCK_LEASE_MS),
          },
        },
        { upsert: true, returnDocument: 'after' }
      )
      const lock = result?.value || result
      if (lock?.owner === owner) return owner
    } catch (error) {
      if (error.code !== 11000) throw error
    }

    await new Promise((resolve) => setTimeout(resolve, 100))
  }

  throw new Error('Timed out waiting for the conversation lock')
}

async function getOrCreateConversation(conversationId) {
  const collection = await getConversationCollection()
  const now = new Date()
  await collection.updateOne(
    { conversation_id: conversationId },
    {
      $setOnInsert: {
        conversation_id: conversationId,
        messages: [],
        security_status: 'clear',
        created_at: now,
        updated_at: now,
      },
    },
    { upsert: true }
  )

  return collection.findOne({ conversation_id: conversationId })
}

async function blockConversation(conversationId) {
  const collection = await getConversationCollection()
  await collection.updateOne(
    { conversation_id: conversationId },
    {
      $set: {
        security_status: 'blocked',
        block_source: 'layer3',
        updated_at: new Date(),
      },
      $setOnInsert: {
        messages: [],
        created_at: new Date(),
      },
    },
    { upsert: true }
  )
}

async function markConversationPending(conversationId) {
  const collection = await getConversationCollection()
  await collection.updateOne(
    { conversation_id: conversationId, security_status: { $ne: 'blocked' } },
    {
      $set: {
        security_status: 'pending',
        updated_at: new Date(),
      },
    }
  )
}

async function refreshPendingStatus(conversationId) {
  const response = await axios.get(
    `${FASTAPI_BASE_URL}/status/${encodeURIComponent(conversationId)}`,
    { timeout: 5000 }
  )
  const status = response.data?.security_status

  if (!['clear', 'blocked', 'pending'].includes(status)) {
    throw new Error('AI engine returned an invalid conversation security status')
  }

  if (status === 'blocked') {
    await blockConversation(conversationId)
    return 'blocked'
  }

  if (status === 'clear') {
    const collection = await getConversationCollection()
    await collection.updateOne(
      { conversation_id: conversationId, security_status: 'pending' },
      { $set: { security_status: 'clear', updated_at: new Date() } }
    )
  }

  const conversation = await getConversationHistory(conversationId)
  return conversation.security_status || 'clear'
}

async function withConversationLock(conversationId, operation) {
  const owner = await acquireConversationLock(conversationId)
  let leaseLost = false
  const renewal = setInterval(async () => {
    try {
      const result = await conversationLockCollection.updateOne(
        { _id: conversationId, owner },
        {
          $set: {
            expires_at: new Date(Date.now() + CONVERSATION_LOCK_LEASE_MS),
          },
        }
      )
      if (result.matchedCount !== 1) leaseLost = true
    } catch (error) {
      leaseLost = true
      console.error('Unable to renew conversation lock:', error.message)
    }
  }, CONVERSATION_LOCK_RENEW_MS)
  renewal.unref()

  try {
    const result = await operation()
    if (leaseLost) throw new Error('Conversation lock lease was lost')
    return result
  } finally {
    clearInterval(renewal)
    if (!leaseLost) {
      await conversationLockCollection.deleteOne({ _id: conversationId, owner })
    }
  }
}

async function appendMessage(conversationId, role, content) {
  const collection = await getConversationCollection()
  const message = {
    role,
    content,
    timestamp: new Date().toISOString(),
  }

  await collection.updateOne(
    { conversation_id: conversationId },
    {
      $push: { messages: message },
      $set: { updated_at: new Date() },
    },
    { upsert: true }
  )
}

async function getConversationHistory(conversationId) {
  const collection = await getConversationCollection()
  const conversation = await collection.findOne({ conversation_id: conversationId })
  return conversation || { conversation_id: conversationId, messages: [], security_status: 'clear' }
}

app.get('/health', async (req, res) => {
  try {
    await getConversationCollection()
    await mongoClient.db(MONGODB_DB).command({ ping: 1 })
    return res.json({ status: 'ok', service: 'prompt-guard-node' })
  } catch (error) {
    return res.status(503).json({
      status: 'degraded',
      service: 'prompt-guard-node',
      message: 'MongoDB is unavailable.',
    })
  }
})

app.post('/conversations', async (req, res) => {
  const requestedId = typeof req.body?.conversation_id === 'string' && req.body.conversation_id.trim()
    ? req.body.conversation_id.trim()
    : randomUUID()

  const conversation = await getOrCreateConversation(requestedId)
  return res.status(201).json(buildConversationSummary(conversation))
})

app.get('/conversations', async (req, res) => {
  try {
    const collection = await getConversationCollection()
    const conversations = await collection
      .find({})
      .sort({ updated_at: -1 })
      .toArray()

    return res.json(conversations.map(buildConversationSummary))
  } catch (error) {
    console.error('Failed to list conversations:', error)
    return res.status(500).json({ message: 'Unable to load conversations.' })
  }
})

app.get('/conversations/:conversation_id', async (req, res) => {
  try {
    const conversation = await getConversationHistory(req.params.conversation_id)
    return res.json(buildConversationSummary(conversation))
  } catch (error) {
    console.error('Failed to fetch conversation:', error)
    return res.status(500).json({ message: 'Unable to load conversation.' })
  }
})

app.get('/internal/conversation-state/:conversation_id', async (req, res) => {
  if (!isInternalRequest(req)) {
    return res.status(403).json({ message: 'Internal endpoint only' })
  }

  try {
    const collection = await getConversationCollection()
    const conversation = await collection.findOne({
      conversation_id: req.params.conversation_id,
    })
    if (!conversation) {
      return res.status(404).json({ message: 'Conversation not found.' })
    }

    return res.json({
      conversation_id: conversation.conversation_id,
      state: getDefaultEngineState(conversation),
    })
  } catch (error) {
    console.error('Failed to load AI conversation state:', error)
    return res.status(500).json({ message: 'Unable to load AI conversation state.' })
  }
})

app.put('/internal/conversation-state/:conversation_id', async (req, res) => {
  if (!isInternalRequest(req)) {
    return res.status(403).json({ message: 'Internal endpoint only' })
  }

  const state = req.body?.state
  const validMessages = Array.isArray(state?.messages) && state.messages.every(
    (message) =>
      message &&
      ['user', 'assistant'].includes(message.role) &&
      typeof message.content === 'string'
  )
  const validState =
    validMessages &&
    Number.isInteger(state.message_count) &&
    state.message_count >= state.messages.length &&
    Number.isInteger(state.checked_up_to) &&
    state.checked_up_to >= 0 &&
    state.checked_up_to <= state.message_count &&
    ['clear', 'blocked', 'pending'].includes(state.security_status)

  if (!validState) {
    return res.status(400).json({ message: 'Invalid AI conversation state.' })
  }

  try {
    const conversationId = req.params.conversation_id
    const collection = await getConversationCollection()
    const engineState = {
      messages: state.messages,
      message_count: state.message_count,
      checked_up_to: state.checked_up_to,
      security_status: state.security_status,
    }

    await getOrCreateConversation(conversationId)
    const conversation = await collection.findOne({ conversation_id: conversationId })
    const storedState = {
      ...engineState,
      security_status: conversation.security_status === 'blocked'
        ? 'blocked'
        : state.security_status,
    }

    if (state.security_status === 'blocked') {
      await collection.updateOne(
        { conversation_id: conversationId },
        {
          $set: {
            engine_state: storedState,
            security_status: 'blocked',
            block_source: 'layer3',
            updated_at: new Date(),
          },
        }
      )
    } else if (conversation.security_status === 'blocked') {
      await collection.updateOne(
        { conversation_id: conversationId, security_status: 'blocked' },
        { $set: { engine_state: storedState, updated_at: new Date() } }
      )
    } else {
      await collection.updateOne(
        { conversation_id: conversationId, security_status: { $ne: 'blocked' } },
        {
          $set: {
            engine_state: storedState,
            security_status: state.security_status,
            updated_at: new Date(),
          },
        }
      )
    }

    return res.json({ status: 'saved' })
  } catch (error) {
    console.error('Failed to persist AI conversation state:', error)
    return res.status(500).json({ message: 'Unable to persist AI conversation state.' })
  }
})

app.post('/conversations/:conversation_id/recheck', async (req, res) => {
  const conversationId = req.params.conversation_id

  try {
    return await withConversationLock(conversationId, async () => {
      const conversation = await getConversationHistory(conversationId)
      if (conversation.security_status !== 'blocked' || conversation.block_source) {
        return res.status(409).json({
          message: 'Only older blocked conversations can be rechecked.',
        })
      }

      const messages = Array.isArray(conversation.messages)
        ? conversation.messages.filter(
          (message) =>
            message &&
            ['user', 'assistant'].includes(message.role) &&
            typeof message.content === 'string'
        )
        : []

      if (messages.length === 0) {
        return res.status(400).json({
          message: 'This conversation has no history to recheck.',
        })
      }

      const response = await axios.post(
        `${FASTAPI_BASE_URL}/layer3-review`,
        { conversation_id: conversationId, messages },
        {
          timeout: 60000,
          headers: INTERNAL_SERVICE_TOKEN
            ? { 'x-internal-token': INTERNAL_SERVICE_TOKEN }
            : {},
        }
      )
      if (typeof response.data?.is_danger !== 'boolean') {
        throw new Error('AI engine returned an invalid Layer 3 review result')
      }

      const collection = await getConversationCollection()
      if (response.data.is_danger) {
        await blockConversation(conversationId)
      } else {
        await collection.updateOne(
          {
            conversation_id: conversationId,
            security_status: 'blocked',
            block_source: { $exists: false },
          },
          {
            $set: {
              security_status: 'clear',
              updated_at: new Date(),
              engine_state: {
                ...getDefaultEngineState(conversation),
                security_status: 'clear',
                checked_up_to: getDefaultEngineState(conversation).message_count,
              },
            },
          }
        )
      }

      const updated = await getConversationHistory(conversationId)
      return res.json({
        conversation_id: conversationId,
        security_status: updated.security_status || 'clear',
        is_danger: response.data.is_danger,
      })
    })
  } catch (error) {
    console.error('Failed to recheck legacy conversation:', error.message)
    return res.status(503).json({
      message: 'Unable to recheck this conversation right now. Please try again later.',
    })
  }
})

async function handleLayer3Alert(req, res) {
  const { conversation_id, is_malicious, reason } = req.body || {}

  if (!isInternalRequest(req)) {
    return res.status(403).json({ message: 'Security alerts are internal only' })
  }

  if (typeof conversation_id !== 'string' || !conversation_id.trim()) {
    return res.status(400).json({ message: 'Invalid conversation_id' })
  }

  if (typeof is_malicious !== 'boolean') {
    return res.status(400).json({ message: 'is_malicious must be a boolean' })
  }

  if (is_malicious === true) {
    await blockConversation(conversation_id)
    console.warn(`Security alert received for ${conversation_id}: ${reason || 'prompt_injection'}`)
  }

  const conversation = await getConversationHistory(conversation_id)
  return res.status(202).json({
    status: is_malicious ? 'alert_received' : 'alert_processed',
    conversation_id,
    security_status: conversation.security_status || 'clear',
  })
}

async function handlePromptDetectionAlert(req, res) {
  const {
    conversation_id,
    is_malicious,
    alert_type,
    layer1_score,
    layer2_score,
    combined_score,
  } = req.body || {}
  const validScores =
    Number.isFinite(layer1_score) &&
    layer1_score >= 0 &&
    layer1_score <= 1 &&
    Number.isFinite(layer2_score) &&
    layer2_score >= -1 &&
    layer2_score <= 1 &&
    Number.isFinite(combined_score) &&
    combined_score >= 0 &&
    combined_score <= 1
  const localRequest = isInternalRequest(req)

  if (
    !localRequest ||
    typeof conversation_id !== 'string' ||
    !conversation_id.trim() ||
    is_malicious !== true ||
    alert_type !== 'prompt_injection' ||
    !validScores
  ) {
    return res.status(localRequest ? 400 : 403).json({
      message: localRequest ? 'Invalid prompt-detection alert' : 'Security alerts are internal only',
    })
  }

  const conversation = await getConversationHistory(conversation_id)
  console.warn(`Layer 1 prompt detection received for ${conversation_id}`)
  return res.status(202).json({
    status: 'alert_received',
    conversation_id,
    security_status: conversation.security_status || 'clear',
  })
}

app.post('/security-alert', handlePromptDetectionAlert)
app.post('/layer3-alert', handleLayer3Alert)

app.post('/llm_response', async (req, res) => {
  const body = req.body || {}
  const promptText = typeof body.prompt === 'string' ? body.prompt : body.user_query
  const conversationId = typeof body.conversation_id === 'string' ? body.conversation_id : null

  if (typeof conversationId !== 'string' || !conversationId.trim()) {
    return res.status(400).json({ message: 'A valid conversation_id is required.' })
  }

  if (typeof promptText !== 'string' || !promptText.trim()) {
    return res.status(400).json({ message: 'Prompt is required.' })
  }

  try {
    return await withConversationLock(conversationId, async () => {
    try {
      const conversation = await getOrCreateConversation(conversationId)

      if (conversation.security_status === 'blocked') {
        return res.status(403).json({
          message: 'This conversation has been blocked due to a security violation.',
          security_status: 'blocked',
        })
      }

      if (conversation.security_status === 'pending') {
        let currentStatus
        try {
          currentStatus = await refreshPendingStatus(conversationId)
        } catch (statusError) {
          console.error('Unable to resolve pending security status:', statusError.message)
          return res.status(503).json({
            message: 'The security status could not be verified. No prompt was sent; please retry shortly.',
            security_status: 'pending',
          })
        }

        if (currentStatus === 'blocked') {
          return res.status(403).json({
            message: 'This conversation has been blocked due to a security violation.',
            security_status: 'blocked',
          })
        }

        if (currentStatus === 'pending') {
          return res.status(503).json({
            message: 'A security check is still in progress. No prompt was sent; please retry shortly.',
            security_status: 'pending',
          })
        }
      }

      const response = await axios.post(
        `${FASTAPI_BASE_URL}/chat`,
        {
          conversation_id: conversationId,
          prompt: promptText.trim(),
        },
        { timeout: 30000 }
      )

      const aiData = response.data || {}
      const answer = aiData.response || aiData.message || 'No response received.'

      await appendMessage(conversationId, 'user', promptText.trim())
      await appendMessage(conversationId, 'assistant', answer)

      if (aiData.l3_result === true) {
        await blockConversation(conversationId)
      }

      const updatedConversation = await getConversationHistory(conversationId)
      const securityStatus = updatedConversation.security_status || 'clear'
      const result = {
        conversation_id: conversationId,
        response: answer,
        message: answer,
        security_status: securityStatus,
      }

      if (aiData.l3_result === true || securityStatus === 'blocked') {
        result.security_warning =
          'Suspicious activity detected. This conversation has been blocked.'
      } else if (aiData.prompt_injection_detected) {
        result.security_warning =
          'Prompt injection was detected. This request was refused; you can continue in this conversation.'
      }

      return res.status(200).json(result)
    } catch (error) {
      if (error.response) {
        const status = error.response.status
        const detail = error.response.data?.detail || {}

        if (status === 403) {
          await blockConversation(conversationId)
          return res.status(403).json({
            message: detail.message || 'This conversation has been blocked due to a security violation.',
            security_status: 'blocked',
          })
        }

        if (status === 503) {
          await markConversationPending(conversationId)
          return res.status(503).json({
            message: detail.message || 'A security check is in progress. Please try again in a moment.',
            security_status: 'pending',
          })
        }
      }

      console.error('LLM request failed:', error.message)
      return res.status(500).json({
        message: 'Unable to process the request right now.',
      })
    }
    })
  } catch (error) {
    console.error('Unable to acquire conversation lock:', error.message)
    return res.status(503).json({
      message: 'This conversation is busy. Please retry shortly.',
    })
  }
})

const port = Number(process.env.PORT) || 5000
app.listen(port, () => {
  console.log(`Node server running on http://localhost:${port}`)
})
