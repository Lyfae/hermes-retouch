// Schedule check for the pane's parallel Send, run on the REAL scheduler code cut out of
// desktop/plugin.js (the pane is one file with no build step, so it cannot be imported apart).
//
// A seeded generator drives random orders of: Sends, gateway replies arriving late or failing,
// agents finishing, failing or stopping half way, turn-ended events arriving late, twice or for
// other sessions, and project refreshes. After every step:
//   S1  never more than MAX_PARALLEL sessions running or being started
//   S2  a mark is never held by two pieces of work at once
//   S3  marks that overlap and were sent together are in the same piece of work
// and once everything has been delivered:
//   S4  nothing is left waiting, starting or running, and no mark is still "sent" with nobody on it
//
// Assumed, not checked: the gateway delivers a turn-ended event for every session it accepted a
// prompt for (possibly late or twice). Without it a failed session's slot is freed only when the
// project shows its marks are no longer busy.
//
// Run:  node verify/pane_scheduler.mjs            (about a second; prints the failing seed and steps)
//       node verify/pane_scheduler.mjs <plugin.js> <runs>
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const file = process.argv[2] || path.join(here, '..', 'desktop', 'plugin.js')
const RUNS = Number(process.argv[3] || 400)
const src = fs.readFileSync(file, 'utf8')

function cut(from, to) {
  const a = src.indexOf(from)
  const b = src.indexOf(to, a + 1)
  if (a < 0 || b < 0) throw new Error(`pane_scheduler: cannot find the scheduler in ${file} (looked for "${from}" … "${to}"). The code moved: update the cut points here.`)
  return src.slice(a, b)
}
const CODE = [
  cut('function bounds(shape, gp) {', 'function translate('),
  cut('const MAX_PARALLEL = 3', '// Which model does the work'),
  cut('async function ownTurnEnded(event) {', '\n}\n') + '\n}\n',
  cut('async function sendToAgent(project, numbers) {', '\n}\n') + '\n}\n'
].join('\n')

const rng = seed => () => {
  seed = (seed + 0x6d2b79f5) | 0
  let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296
}
const flush = async () => {
  for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r))
}
const BUSY = new Set(['queued', 'working'])
const touches = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h

async function run(seed) {
  const rand = rng(seed)
  const pick = list => list[Math.floor(rand() * list.length)]
  const log = []
  const pins = Array.from({ length: 8 }, (_, i) => ({
    number: i + 1,
    shape: 'rect',
    geom: { x: Math.floor(rand() * 80), y: Math.floor(rand() * 80), w: 8 + Math.floor(rand() * 30), h: 8 + Math.floor(rand() * 30) },
    status: 'open'
  }))
  const pin = n => pins.find(p => p.number === n)
  const snapshot = () => ({ id: 'p', title: 't', pins: pins.map(p => ({ ...p, geom: { ...p.geom } })) })
  const pending = [] // gateway calls not answered yet
  const live = [] // sessions that accepted a prompt: { session, pins }
  const chat = [] // marks the open chat took in the fallback
  const events = [] // turn-ended events not delivered yet
  let made = 0

  const rest = async (url, opts = {}) => {
    if (url.endsWith('/send')) {
      const took = (opts.body.pins || []).filter(n => pin(n).status === 'open')
      if (!took.length) throw new Error('no open marks to send')
      took.forEach(n => (pin(n).status = 'queued'))
      return { queued: took, prompt: `chat:${took.join(',')}`, own_session_prompt: 'own' }
    }
    const act = url.match(/\/pins\/(\d+)\/action$/)
    if (act) {
      const p = pin(Number(act[1]))
      if (p.status !== 'queued') throw new Error('409')
      p.status = 'open'
      return p
    }
    return snapshot()
  }
  const host = {
    request: (method, params) =>
      new Promise((resolve, reject) => pending.push({ method, params, resolve, reject })),
    composer: {
      submit: async (_session, prompt) => {
        const r = rand()
        if (r < 0.2) throw new Error('no composer')
        if (r >= 0.7) return false
        chat.push(...prompt.slice(5).split(',').map(Number)) // the open chat's agent has these now
        return true
      }
    },
    state: { activeSessionId: { get: () => null } },
    notify: () => {}
  }
  const env = {
    host,
    rest,
    refresh: () => {},
    chosenModel: () => null,
    stamp: () => 't',
    statusOf: s => ({ busy: BUSY.has(s) }),
    act: async fn => {
      try {
        return (await fn()) ?? true
      } catch {
        return null
      }
    }
  }
  const api = new Function(...Object.keys(env), `${CODE}\nreturn { sendToAgent, settle, ownTurnEnded, inFlight, waiting, launching, MAX_PARALLEL }`)(...Object.values(env))

  const work = () => [...api.inFlight.values(), ...api.waiting, ...api.launching]
  const check = step => {
    const fail = why => {
      throw new Error(`${why}\n  seed ${seed}, after step ${step}:\n    ${log.slice(-14).join('\n    ')}`)
    }
    if (api.inFlight.size + api.launching.size > api.MAX_PARALLEL)
      fail(`S1: ${api.inFlight.size} sessions running and ${api.launching.size} being started; the limit is ${api.MAX_PARALLEL}`)
    const seen = new Set()
    for (const w of work()) for (const n of w.pins) seen.has(n) ? fail(`S2: mark #${n} is held by two pieces of work`) : seen.add(n)
  }
  const answer = ok => {
    if (!pending.length) return
    const call = pending.splice(Math.floor(rand() * pending.length), 1)[0]
    log.push(`${call.method} ${ok ? 'answered' : 'failed'}`)
    if (!ok) return call.reject(new Error('gateway said no'))
    if (call.method === 'session.create') return call.resolve({ session_id: `s${++made}` })
    live.push({ session: call.params.session_id, pins: [...(api.inFlight.get(call.params.session_id)?.pins || [])] })
    call.resolve({})
  }
  const agent = how => {
    if (!live.length) return
    const s = live.splice(Math.floor(rand() * live.length), 1)[0]
    log.push(`agent in ${s.session} ${how} on ${s.pins.map(n => `#${n}`).join(',')}`)
    if (how === 'finishes') s.pins.forEach(n => BUSY.has(pin(n).status) && (pin(n).status = rand() < 0.3 ? 'question' : 'needs_review'))
    if (how === 'stops half way') s.pins.forEach((n, i) => BUSY.has(pin(n).status) && (pin(n).status = i ? 'needs_review' : 'working'))
    const event = { type: how === 'errors' ? 'error' : 'message.complete', session_id: s.session, payload: how === 'finishes' ? {} : { error: 'boom' } }
    events.push(event)
    if (rand() < 0.2) events.push(event) // the same event twice
  }
  const deliver = async () => {
    if (!events.length) return
    const e = events.splice(Math.floor(rand() * events.length), 1)[0]
    log.push(`event ${e.type} for ${e.session_id}`)
    await api.ownTurnEnded(e)
  }
  const send = async () => {
    const open = pins.filter(p => p.status === 'open')
    if (!open.length) return
    const some = open.filter(() => rand() < 0.6)
    const chosen = some.length ? some : [pick(open)]
    log.push(`Send ${chosen.map(p => `#${p.number}`).join(',')}`)
    const before = new Set(work())
    await api.sendToAgent(snapshot(), chosen.map(p => p.number))
    // S3, judged on the work this Send created
    const jobs = work().filter(w => !before.has(w))
    for (const a of chosen) for (const b of chosen) {
      if (a.number >= b.number || !touches(a.geom, b.geom)) continue
      const ja = jobs.find(j => j.pins.includes(a.number))
      const jb = jobs.find(j => j.pins.includes(b.number))
      if (ja && jb && ja !== jb) throw new Error(`S3: marks #${a.number} and #${b.number} overlap but were sent as separate work\n  seed ${seed}:\n    ${log.slice(-8).join('\n    ')}`)
    }
  }
  for (let step = 0; step < 70; step++) {
    const r = rand()
    if (r < 0.22) await send()
    else if (r < 0.5) answer(rand() < 0.8)
    else if (r < 0.66) agent(pick(['finishes', 'finishes', 'errors', 'stops half way']))
    else if (r < 0.8) await deliver()
    else if (r < 0.9) (log.push('refresh'), api.settle(snapshot()))
    else if (r < 0.95) (log.push('stray event'), await api.ownTurnEnded({ type: 'message.complete', session_id: 'someone-else', payload: {} }))
    else {
      // The user sends a mark back from what the pane shows, and the pane shows a status only
      // after a refresh, so a refresh always comes first (Workbench's effect calls settle).
      const done = pins.filter(p => p.status === 'needs_review' || p.status === 'question')
      if (done.length) api.settle(snapshot()), (pick(done).status = 'open'), log.push('refresh, then the user reopens a mark')
    }
    await flush()
    check(step)
  }
  // everything outstanding is delivered
  for (let round = 0; round < 200 && (pending.length || live.length || events.length || api.waiting.length || api.launching.size); round++) {
    while (pending.length) answer(rand() < 0.8), await flush()
    while (live.length) agent(pick(['finishes', 'errors', 'stops half way']))
    while (events.length) await deliver()
    api.settle(snapshot())
    await flush()
    check(`drain ${round}`)
  }
  const stuck = pins.filter(p => p.status === 'queued' && !chat.includes(p.number))
  const left = `waiting ${api.waiting.length}, starting ${api.launching.size}, running ${api.inFlight.size}`
  if (api.waiting.length || api.launching.size || api.inFlight.size) throw new Error(`S4: work is left over after everything was delivered (${left})\n  seed ${seed}:\n    ${log.slice(-14).join('\n    ')}`)
  // A mark still "sent" is fine only when the fallback handed it to the open chat (its agent owns it).
  if (stuck.length) throw new Error(`S4: ${stuck.map(p => `#${p.number}`).join(', ')} still "sent" with nobody working on it\n  seed ${seed}:\n    ${log.slice(-14).join('\n    ')}`)
  return { made }
}

let sessions = 0
for (let seed = 1; seed <= RUNS; seed++) sessions += (await run(seed)).made
console.log(`pane scheduler: ${RUNS} random schedules, ${sessions} sessions started, S1–S4 held`)
