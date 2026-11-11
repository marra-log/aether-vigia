// Conector de WhatsApp por QR Code (Baileys) do Aether Vigia.
// Mantém a sessão em AUTH_DIR, envia alertas e repassa as respostas do dono ao servidor.
import fs from 'node:fs/promises'
import http from 'node:http'
import crypto from 'node:crypto'
import pino from 'pino'
import QRCode from 'qrcode'
import makeWASocket, {
  Browsers, DisconnectReason, fetchLatestBaileysVersion, useMultiFileAuthState,
} from 'baileys'

const PORT = Number(process.env.PORT || 3000)
const KEY = process.env.GATEWAY_KEY || ''
const WEBHOOK_URL = process.env.WEBHOOK_URL || ''
const AUTH_DIR = process.env.AUTH_DIR || './auth'
const MAX_BODY = 4 * 1024 * 1024
const log = pino({ level: process.env.LOG_LEVEL || 'info' })

if (KEY.length < 16) {
  log.error('Defina GATEWAY_KEY com pelo menos 16 caracteres')
  process.exit(1)
}

const state = { status: 'starting', qr: null, number: null, since: Date.now() }
let sock = null
let retry = 0
let restarting = false
let generation = 0   // só a conexão mais recente pode reagir a eventos

function setStatus(status, extra = {}) {
  Object.assign(state, { status, since: Date.now() }, extra)
}

async function connect() {
  const mine = ++generation
  const { state: auth, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
  let version
  try {
    ({ version } = await fetchLatestBaileysVersion())
  } catch {
    log.warn('Não consegui buscar a versão do WhatsApp Web; usando a padrão')
  }
  setStatus('connecting', { qr: null })
  sock = makeWASocket({
    auth,
    version,
    browser: Browsers.ubuntu('Aether Vigia'),
    logger: pino({ level: 'silent' }),
    markOnlineOnConnect: false,
    syncFullHistory: false,
  })
  sock.ev.on('creds.update', saveCreds)

  sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
    if (mine !== generation) return
    if (qr) {
      setStatus('qr', { qr: await QRCode.toDataURL(qr, { margin: 1, width: 360 }), number: null })
      log.info('QR Code disponível: abra o painel para conectar')
    }
    if (connection === 'open') {
      retry = 0
      setStatus('open', { qr: null, number: (sock.user?.id || '').split(/[:@]/)[0] })
      log.info('WhatsApp conectado')
    }
    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode
      if (code === DisconnectReason.loggedOut) {
        log.warn('Sessão encerrada no celular: será preciso ler o QR Code de novo')
        await resetSession()
        return
      }
      setStatus('closed', { qr: null })
      const wait = Math.min(2000 * 2 ** retry++, 60000)
      log.warn({ code, wait }, 'Conexão caiu, tentando de novo')
      setTimeout(() => {
        if (mine === generation) connect().catch((e) => log.error(e, 'Falha ao reconectar'))
      }, wait)
    }
  })

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify' || mine !== generation) return
    for (const m of messages) {
      try {
        await forward(m)
      } catch (e) {
        log.error(e, 'Falha ao repassar mensagem')
      }
    }
  })
}

// Só repassa texto de conversa individual; nunca mensagens próprias ou de grupo.
async function forward(m) {
  const key = m.key || {}
  if (key.fromMe || !m.message) return
  const jid = [key.remoteJid, key.remoteJidAlt].find((j) => j?.endsWith('@s.whatsapp.net'))
  if (!jid) return
  const text = m.message.conversation || m.message.extendedTextMessage?.text || ''
  if (!text || !WEBHOOK_URL) return
  const res = await fetch(WEBHOOK_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ phone: jid.split('@')[0].split(':')[0], text: text.slice(0, 500) }),
    signal: AbortSignal.timeout(15000),
  })
  if (!res.ok) log.warn({ status: res.status }, 'Servidor recusou a mensagem recebida')
}

async function resetSession() {
  if (restarting) return
  restarting = true
  generation++
  try {
    try { sock?.end?.(undefined) } catch { /* já fechado */ }
    // O diretório pode ser a raiz de um volume: apaga o conteúdo, não a pasta.
    for (const name of await fs.readdir(AUTH_DIR).catch(() => [])) {
      await fs.rm(`${AUTH_DIR}/${name}`, { recursive: true, force: true })
    }
    retry = 0
    await connect()
  } finally {
    restarting = false
  }
}

// Celulares do Brasil podem estar registrados com ou sem o nono dígito.
async function resolveJid(number) {
  const digits = String(number).replace(/\D/g, '')
  if (digits.length < 10 || digits.length > 15) throw httpError(422, 'Número inválido')
  const options = [digits]
  if (digits.startsWith('55') && digits.length === 13 && digits[4] === '9') {
    options.push(digits.slice(0, 4) + digits.slice(5))
  } else if (digits.startsWith('55') && digits.length === 12) {
    options.push(digits.slice(0, 4) + '9' + digits.slice(4))
  }
  const found = await sock.onWhatsApp(...options)
  const hit = (found || []).find((r) => r.exists)
  if (!hit) throw httpError(422, 'Este número não tem WhatsApp')
  return hit.jid
}

async function send({ number, text, image_b64: image }) {
  if (state.status !== 'open') throw httpError(503, 'WhatsApp desconectado')
  if (typeof text !== 'string' || !text.trim()) throw httpError(422, 'Texto obrigatório')
  const jid = await resolveJid(number)
  const content = image
    ? { image: Buffer.from(image, 'base64'), caption: text, mimetype: 'image/jpeg' }
    : { text }
  const sent = await sock.sendMessage(jid, content)
  return { id: sent?.key?.id || '' }
}

function httpError(status, message) {
  return Object.assign(new Error(message), { status })
}

function authorized(req) {
  const given = Buffer.from((req.headers.authorization || '').replace(/^Bearer\s+/i, ''))
  const expected = Buffer.from(KEY)
  return given.length === expected.length && crypto.timingSafeEqual(given, expected)
}

async function readJson(req) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > MAX_BODY) throw httpError(413, 'Mensagem grande demais')
    chunks.push(chunk)
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}')
  } catch {
    throw httpError(400, 'JSON inválido')
  }
}

const server = http.createServer(async (req, res) => {
  const reply = (status, body) => {
    res.writeHead(status, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify(body))
  }
  try {
    const path = new URL(req.url, 'http://x').pathname
    if (req.method === 'GET' && path === '/health') return reply(200, { status: 'ok' })
    if (!authorized(req)) return reply(401, { error: 'Chave inválida' })
    if (req.method === 'GET' && path === '/status') return reply(200, state)
    if (req.method === 'POST' && path === '/send') return reply(200, await send(await readJson(req)))
    if (req.method === 'POST' && path === '/logout') {
      if (state.status === 'open') await sock.logout().catch(() => {})
      await resetSession()
      return reply(200, { ok: true })
    }
    return reply(404, { error: 'Não encontrado' })
  } catch (e) {
    if (!e.status) log.error(e, 'Erro inesperado')
    return reply(e.status || 500, { error: e.status ? e.message : 'Erro interno' })
  }
})

await fs.mkdir(AUTH_DIR, { recursive: true })
// '::' atende IPv4 e IPv6 (rede privada da Railway); sem IPv6 na máquina, cai para IPv4.
server.once('error', (e) => {
  if (e.code !== 'EAFNOSUPPORT') throw e
  server.listen(PORT, '0.0.0.0', () => log.info(`Conector ouvindo na porta ${PORT} (IPv4)`))
})
server.listen(PORT, '::', () => log.info(`Conector ouvindo na porta ${PORT}`))
connect().catch((e) => {
  log.error(e, 'Falha ao iniciar a conexão')
  process.exit(1)
})
