/**
 * Retouch — Desktop half. The image fills the pane; a floating toolbar (select, pen, shape, note)
 * draws marks on it. Each mark carries a note and its own thread with Hermes, which edits only inside
 * the marks. The user reviews each result in the mark's bubble: accept, needs more work, or revert.
 *
 * Plain ESM loaded uncompiled: jsx() calls, imports only from the plugin SDK and React. Marks are drawn
 * on an SVG whose viewBox is the image's FULL-resolution pixel grid, so geometry is already in image
 * pixels at any pane size. Layout uses inline styles with theme variables: the app's stylesheet only
 * carries the utility classes core itself uses.
 */

import {
  Button,
  captureGatewayFileDownload,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  GlyphSpinner,
  host,
  icons,
  KEYBINDS_AREA,
  PALETTE_AREA,
  TITLEBAR_AREAS,
  Popover,
  PopoverContent,
  PopoverTrigger,
  queryClient,
  Textarea,
  Tip,
  useQuery
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const QK = ['retouch']
const EVENT = 'plugin.retouch.project.updated'
const INTENTS = [
  ['fix', 'Fix'],
  ['remove', 'Remove'],
  ['enhance', 'Enhance'],
  ['ask', 'Ask']
]
const PLACEHOLDER = {
  fix: 'What should change here?',
  remove: 'What should go, and what belongs there instead?',
  enhance: 'What should look better?',
  ask: 'What do you want to know about this area?'
}
const STATUS = {
  open: { label: 'Not sent yet', color: 'var(--ui-accent)' },
  queued: { label: 'Sent', color: 'var(--ui-accent)', busy: true },
  working: { label: 'Working', color: 'var(--ui-accent)', busy: true },
  needs_review: { label: 'Ready for review', color: 'var(--ui-orange)' },
  question: { label: 'Question for you', color: 'var(--ui-orange)' },
  resolved: { label: 'Accepted', color: 'var(--ui-green)', closed: true },
  wont_fix: { label: "Won't fix", color: 'var(--ui-text-quaternary)', closed: true }
}
const statusOf = s => STATUS[s] || { label: s, color: 'var(--ui-text-tertiary)' }

const BRUSH = 0.03 // pen width as a fraction of the image's shorter side
const NOTE_R = 0.05 // radius of the area a text note covers, same unit
const MIN_MARK = 0.01 // smallest box/ellipse side, fraction of the longer side
const FORMATS = [
  ['png', 'PNG', 'Lossless'],
  ['jpeg', 'JPEG', 'Smaller file'],
  ['webp', 'WebP', 'Smallest file']
]
const WARN = {
  border: '1px solid color-mix(in srgb, var(--ui-orange) 55%, transparent)',
  background: 'color-mix(in srgb, var(--ui-orange) 10%, transparent)',
  borderRadius: 6,
  padding: '5px 8px',
  fontSize: 12,
  lineHeight: '16px'
}
const TYPING = 'input, textarea, [contenteditable="true"]'

let api = null // the plugin ctx, set in register()

const rest = (path, opts) => api.rest(path, opts)
const refresh = () => queryClient.invalidateQueries({ queryKey: QK })

// ctx.rest rejects with "<status>: <body>", which the IPC bridge wraps as "Error invoking remote
// method 'hermes:api': Error: 409: {"detail": …}". Show the backend's own sentence, not the wire text.
function errorText(error) {
  const message = String(error?.message ?? error)
  const status = /(?:^|error:\s*)\d{3}:\s*([\s\S]*)$/i.exec(message)
  if (!status) return message
  try {
    const { detail } = JSON.parse(status[1])
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) return detail.map(d => d.msg).join('; ') // FastAPI validation errors
  } catch {
    // a non-JSON body (proxy page, plain text) is already the best text there is
  }
  return status[1] || message
}

async function act(fn) {
  try {
    const out = await fn()
    await refresh()
    return out ?? true
  } catch (error) {
    host.notify({ kind: 'error', message: `Retouch: ${errorText(error)}` })
    return null
  }
}

// --- glyphs the app's icon set does not have ---------------------------------------------------

function glyph(d) {
  return function Glyph() {
    return jsx('svg', {
      width: 16,
      height: 16,
      viewBox: '0 0 24 24',
      fill: 'none',
      stroke: 'currentColor',
      strokeWidth: 1.75,
      strokeLinecap: 'round',
      strokeLinejoin: 'round',
      'aria-hidden': true,
      children: jsx('path', { d })
    })
  }
}
const PointerGlyph = glyph('M6.5 3.5v15l4-3.9 2.8 6.3 2.3-1-2.8-6.2h5.7z')
const LassoGlyph = glyph('M12 4.5c4.4 0 8 2.1 8 4.8s-3.6 4.8-8 4.8-8-2.1-8-4.8 3.6-4.8 8-4.8zM7.8 13.4c-1.1 1.3-.7 2.9.7 3.5 1.2.5 1.4 1.9.5 3.1')
const TextGlyph = glyph('M5.5 7V5h13v2M12 5v14M9.5 19h5')
const UndoGlyph = glyph('M9 14 4.5 9.5 9 5M4.5 9.5H15a5 5 0 0 1 0 10h-3')
const icon = (Icon, size = 16) => jsx(Icon, { size, stroke: 1.75 })

const SHAPES = {
  rect: { label: 'Box', key: 'r', Glyph: () => icon(icons.Square) },
  ellipse: { label: 'Ellipse', key: 'e', Glyph: () => icon(icons.CircleIcon) },
  lasso: { label: 'Lasso', key: 'l', Glyph: LassoGlyph }
}

// --- geometry (pixel space of the full-resolution image) ---------------------------------------

function toPx(shape, g, W, H) {
  if (shape === 'ellipse' || shape === 'rect') return { x: g.x * W, y: g.y * H, w: g.w * W, h: g.h * H }
  const out = { points: g.points.map(([x, y]) => [x * W, y * H]) }
  if (shape === 'brush') out.width = g.width * Math.min(W, H)
  return out
}

function bounds(shape, gp) {
  if (!gp.points) return gp
  const xs = gp.points.map(p => p[0])
  const ys = gp.points.map(p => p[1])
  const pad = shape === 'brush' ? gp.width / 2 : 0
  const x = Math.min(...xs) - pad
  const y = Math.min(...ys) - pad
  return { x, y, w: Math.max(...xs) + pad - x, h: Math.max(...ys) + pad - y }
}

function translate(shape, gp, dx, dy, W, H) {
  const b = bounds(shape, gp)
  const tx = Math.max(-b.x, Math.min(W - (b.x + b.w), dx))
  const ty = Math.max(-b.y, Math.min(H - (b.y + b.h), dy))
  if (gp.points) return { ...gp, points: gp.points.map(([x, y]) => [x + tx, y + ty]) }
  return { ...gp, x: gp.x + tx, y: gp.y + ty }
}

const boxFrom = (a, b) => ({ x: Math.min(a[0], b[0]), y: Math.min(a[1], b[1]), w: Math.abs(a[0] - b[0]), h: Math.abs(a[1] - b[1]) })
const pointsAttr = pts => pts.map(([x, y]) => `${x},${y}`).join(' ')
const pct = (v, total) => `${(100 * v) / total}%`

/** Where a mark's number sits: on its outline, upper left. */
function badgeAt(shape, gp) {
  if (shape === 'ellipse') return [gp.x + (gp.w / 2) * (1 - Math.SQRT1_2), gp.y + (gp.h / 2) * (1 - Math.SQRT1_2)]
  if (shape === 'rect') return [gp.x, gp.y]
  if (shape === 'brush') return gp.points[0]
  return gp.points.reduce((top, p) => (p[1] < top[1] || (p[1] === top[1] && p[0] < top[0]) ? p : top))
}

function Shape({ shape, gp, color, selected = false, dashed = false, busy = false, faded = false, hit = false, pin }) {
  const base = { 'data-pin': pin, style: { pointerEvents: hit ? 'all' : 'none', cursor: hit ? 'pointer' : undefined } }
  if (shape === 'brush') {
    const opacity = faded ? 0.15 : selected ? 0.55 : 0.35
    if (gp.points.length === 1)
      return jsx('circle', {
        ...base,
        cx: gp.points[0][0],
        cy: gp.points[0][1],
        r: gp.width / 2,
        style: { ...base.style, fill: color, fillOpacity: opacity }
      })
    return jsx('polyline', {
      ...base,
      points: pointsAttr(gp.points),
      fill: 'none',
      strokeWidth: gp.width,
      strokeLinecap: 'round',
      strokeLinejoin: 'round',
      style: { ...base.style, stroke: color, strokeOpacity: opacity, pointerEvents: hit ? 'stroke' : 'none' }
    })
  }
  const props = {
    ...base,
    vectorEffect: 'non-scaling-stroke',
    strokeWidth: selected ? 3 : 2,
    strokeDasharray: dashed || busy ? '7 5' : faded ? '2 4' : undefined,
    style: {
      ...base.style,
      stroke: color,
      strokeOpacity: faded ? 0.6 : 1,
      fill: color,
      fillOpacity: faded ? 0 : selected ? 0.14 : 0.07
    },
    children: busy ? jsx('animate', { attributeName: 'stroke-dashoffset', from: '0', to: '-24', dur: '0.9s', repeatCount: 'indefinite' }) : null
  }
  if (shape === 'ellipse') return jsx('ellipse', { ...props, cx: gp.x + gp.w / 2, cy: gp.y + gp.h / 2, rx: gp.w / 2, ry: gp.h / 2 })
  if (shape === 'rect') return jsx('rect', { ...props, x: gp.x, y: gp.y, width: gp.w, height: gp.h, rx: 2 })
  return jsx('polygon', { ...props, points: pointsAttr(gp.points) })
}

// --- data --------------------------------------------------------------------------------------

function useProject(id) {
  return useQuery({
    queryKey: [...QK, 'project', id],
    queryFn: () => rest(`/projects/${id}`),
    enabled: Boolean(id),
    refetchInterval: q => ((q.state.data?.pins || []).some(p => statusOf(p.status).busy) ? 3000 : false)
  })
}

function useImage(id, version, enabled = true) {
  return useQuery({
    queryKey: [...QK, 'image', id, version],
    queryFn: () => rest(`/projects/${id}/image?version=${encodeURIComponent(version)}&max_px=2048`),
    enabled: Boolean(id && version && enabled),
    staleTime: Infinity,
    placeholderData: previous => previous
  })
}

function imageFrom(transfer) {
  const files = [...(transfer?.files || [])]
  return files.find(f => f.type.startsWith('image/') || /\.(heic|heif)$/i.test(f.name)) || null
}

async function upload(file, onOpened) {
  if (!file) return
  const bytes = await file.arrayBuffer()
  const session = host.state.activeSessionId.get()
  const query = session ? `?session_id=${encodeURIComponent(session)}` : ''
  const project = await act(() =>
    rest(`/projects${query}`, {
      method: 'POST',
      upload: { filename: file.name || 'pasted.png', contentType: file.type || 'application/octet-stream', bytes },
      timeoutMs: 120000
    })
  )
  if (project?.id) onOpened(project.id)
}

// The OS region picker runs on the gateway host; the capture opens as a scratch project the user
// keeps (Keep, Save, or by marking or editing it) or discards. Resolves to nothing when they press
// Escape in the picker.
async function takeScreenshot(onOpened) {
  const session = host.state.activeSessionId.get()
  const res = await act(() => rest('/screenshot', { method: 'POST', body: { session_id: session || null }, timeoutMs: 65000 }))
  if (res?.project?.id) onOpened(res.project.id)
}

// Is the active profile served by a gateway on THIS machine? Unknown (older Desktop) counts as no.
async function gatewayIsLocal() {
  if (typeof host.profileRoutes !== 'function') return false
  try {
    const routes = await host.profileRoutes()
    const profile = host.state.profile?.get?.()
    const route = routes.find(r => r.targetProfile === profile || r.profile === profile) || routes.find(r => r.primary)
    return route?.mode === 'local'
  } catch {
    return false
  }
}

// The picker runs on the GATEWAY host. Offer it only when that host is this machine (a local
// route) and it has the tool; a remote gateway's crosshair would appear on someone else's screen.
function useCanScreenshot() {
  const caps = useQuery({
    queryKey: [...QK, 'capabilities'],
    queryFn: async () => {
      const [server, local] = await Promise.all([rest('/capabilities'), gatewayIsLocal()])
      return { screenshot: Boolean(server?.screenshot) && local }
    },
    staleTime: Infinity
  })
  return Boolean(caps.data?.screenshot)
}

async function sendToAgent(project, numbers) {
  const open = project.pins.filter(p => p.status === 'open' && (!numbers || numbers.includes(p.number))).map(p => p.number)
  if (!open.length) return
  // Queue first, then hand the chat the request: an agent that starts instantly must find the marks queued.
  const queued = await act(() => rest(`/projects/${project.id}/send`, { method: 'POST', body: { pins: open } }))
  if (!queued?.prompt) return
  const sent = await host.composer.submit(host.state.activeSessionId.get() ?? null, queued.prompt)
  if (sent) return
  await act(() =>
    Promise.all(queued.queued.map(n => rest(`/projects/${project.id}/pins/${n}/action`, { method: 'POST', body: { action: 'unsend' } })))
  )
  host.notify({ kind: 'warning', message: 'Retouch: open a chat next to this pane, then send again' })
}

async function exportImage(project, format) {
  const save = captureGatewayFileDownload() // must be captured inside the click, before awaiting
  const out = await act(() => rest(`/projects/${project.id}/export`, { method: 'POST', body: { format } }))
  if (out?.path) await save(out.path, out.name)
}

// --- opening an image --------------------------------------------------------------------------

function FilePicker({ inputRef, onOpened }) {
  return jsx('input', {
    ref: inputRef,
    type: 'file',
    accept: 'image/*,.heic,.heif',
    style: { display: 'none' },
    onChange: e => {
      upload(e.target.files?.[0], onOpened)
      e.target.value = ''
    }
  })
}

function RecentRow({ project, onOpened }) {
  return jsxs('button', {
    type: 'button',
    onClick: () => onOpened(project.id),
    onMouseEnter: e => (e.currentTarget.style.background = 'var(--ui-row-hover-background)'),
    onMouseLeave: e => (e.currentTarget.style.background = 'transparent'),
    style: {
      display: 'flex',
      justifyContent: 'space-between',
      gap: 12,
      padding: '6px 8px',
      borderRadius: 6,
      border: 'none',
      background: 'transparent',
      color: 'var(--ui-text-secondary)',
      cursor: 'pointer',
      font: 'inherit',
      textAlign: 'left'
    },
    children: [
      jsx('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: project.title }),
      jsx('span', {
        style: { color: 'var(--ui-text-quaternary)', flexShrink: 0 },
        children: project.open_marks ? `${project.open_marks} open` : 'done'
      })
    ]
  })
}

function Start({ onOpened }) {
  const input = useRef(null)
  const [over, setOver] = useState(false)
  const { data } = useQuery({ queryKey: [...QK, 'projects'], queryFn: () => rest('/projects') })
  const canShoot = useCanScreenshot()
  const [shooting, setShooting] = useState(false)
  const recent = (data?.projects || []).slice(0, 6)
  return jsxs('div', {
    tabIndex: 0,
    style: {
      height: '100%',
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      gap: 14,
      padding: 24,
      outline: 'none',
      fontSize: 13
    },
    onDragOver: e => {
      e.preventDefault()
      setOver(true)
    },
    onDragLeave: () => setOver(false),
    onDrop: e => {
      e.preventDefault()
      setOver(false)
      upload(imageFrom(e.dataTransfer), onOpened)
    },
    onPaste: e => upload(imageFrom(e.clipboardData), onOpened),
    children: [
      jsx(FilePicker, { inputRef: input, onOpened }),
      jsxs('div', {
        style: {
          width: '100%',
          maxWidth: 360,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          gap: 10,
          padding: '28px 20px',
          borderRadius: 12,
          border: `1.5px dashed ${over ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)'}`,
          background: over ? 'color-mix(in srgb, var(--ui-accent) 6%, transparent)' : 'transparent',
          textAlign: 'center'
        },
        children: [
          jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: icon(icons.ImageIcon, 28) }),
          jsx('div', { style: { fontSize: 15, fontWeight: 600, color: 'var(--ui-text-primary)' }, children: 'Retouch' }),
          jsx('div', {
            style: { color: 'var(--ui-text-tertiary)', lineHeight: '18px' },
            children: 'Mark what needs work and say what you want. Hermes changes only the areas you mark, and every edit can be undone.'
          }),
          jsxs('div', {
            style: { display: 'flex', gap: 8 },
            children: [
              jsx(Button, { size: 'sm', onClick: () => input.current?.click(), children: 'Open an image' }),
              canShoot &&
                jsxs(Button, {
                  size: 'sm',
                  variant: 'secondary',
                  disabled: shooting,
                  onClick: async () => {
                    setShooting(true)
                    try {
                      await takeScreenshot(onOpened)
                    } finally {
                      setShooting(false)
                    }
                  },
                  children: [shooting ? jsx(GlyphSpinner, { ariaLabel: 'Pick a region' }) : icon(icons.Monitor), shooting ? 'Pick a region…' : 'Screenshot']
                })
            ]
          }),
          jsx('div', {
            style: { fontSize: 12, color: 'var(--ui-text-quaternary)' },
            children: canShoot
              ? 'or drop one here, or paste with ⌘V · a screenshot is kept only if you mark, edit, Keep or Save it'
              : 'or drop one here, or paste with ⌘V (⌘⇧⌃4 copies a region of the screen)'
          })
        ]
      }),
      recent.length > 0 &&
        jsxs('div', {
          style: { width: '100%', maxWidth: 360, display: 'flex', flexDirection: 'column', gap: 2 },
          children: [
            jsx('div', { style: { fontSize: 11, color: 'var(--ui-text-quaternary)', padding: '0 8px 4px' }, children: 'Recent' }),
            ...recent.map(p => jsx(RecentRow, { project: p, onOpened }, p.id))
          ]
        })
    ]
  })
}

// --- bubbles -----------------------------------------------------------------------------------

function DraftBubble({ project, draft, onAdded, onCancel }) {
  const [intent, setIntent] = useState(draft.intent || 'fix')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const add = async () => {
    if (!note.trim() || busy) return
    setBusy(true)
    const pin = await act(() =>
      rest(`/projects/${project.id}/pins`, { method: 'POST', body: { shape: draft.shape, geom: draft.geom, intent, note } })
    )
    setBusy(false)
    if (pin?.number) onAdded(pin.number)
  }
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 8 },
    // Anywhere in the bubble: picking the intent after typing moves focus to its chip.
    onKeyDown: e => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault()
        add()
      }
    },
    children: [
      jsx('div', { style: { fontSize: 12, fontWeight: 600, color: 'var(--ui-text-secondary)' }, children: 'New mark' }),
      jsx('div', {
        style: { display: 'flex', gap: 4, flexWrap: 'wrap' },
        children: INTENTS.map(([value, label]) =>
          jsx(Button, { size: 'xs', variant: intent === value ? 'secondary' : 'ghost', onClick: () => setIntent(value), children: label }, value)
        )
      }),
      jsx(Textarea, {
        'data-retouch-note': true,
        rows: 3,
        placeholder: PLACEHOLDER[intent],
        value: note,
        onChange: e => setNote(e.target.value)
      }),
      jsxs('div', {
        style: { display: 'flex', alignItems: 'center', gap: 4 },
        children: [
          jsx('span', { style: { flex: 1, fontSize: 11, color: 'var(--ui-text-quaternary)' }, children: '⌘↩ to add' }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: onCancel, children: 'Cancel' }),
          jsx(Button, { size: 'xs', disabled: !note.trim() || busy, onClick: add, children: 'Add mark' })
        ]
      })
    ]
  })
}

function Message({ m }) {
  const agent = m.author === 'agent'
  return jsxs('div', {
    style: {
      display: 'flex',
      flexDirection: 'column',
      gap: 4,
      padding: agent ? '6px 8px' : '2px 0',
      borderRadius: 6,
      background: agent ? 'var(--ui-bg-quaternary)' : 'transparent',
      fontSize: 12,
      lineHeight: '17px',
      color: agent ? 'var(--ui-text-primary)' : 'var(--ui-text-secondary)'
    },
    children: [
      jsxs('span', { children: [jsx('span', { style: { fontWeight: 600 }, children: agent ? 'Hermes ' : 'You ' }), m.body] }),
      m.issues && jsx('div', { style: WARN, children: `Still visible: ${m.issues}` })
    ]
  })
}

function pinButtons({ project, pin, reply, action, onClose }) {
  const st = statusOf(pin.status)
  const base = `/projects/${project.id}/pins/${pin.number}`
  const buttons = []
  if (pin.status === 'open')
    buttons.push(
      jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => act(() => rest(base, { method: 'DELETE' })).then(onClose), children: 'Delete' }, 'del'),
      jsx(Button, { size: 'xs', onClick: () => sendToAgent(project, [pin.number]), children: `Send #${pin.number}` }, 'send')
    )
  if (pin.status === 'needs_review' || pin.status === 'question') {
    const answering = pin.status === 'question'
    buttons.push(
      jsx(Button, { size: 'xs', onClick: () => action('accept', reply), children: 'Accept' }, 'acc'),
      jsx(
        Tip,
        {
          label: reply.trim() ? 'Send it back to Hermes with your reply' : 'Write your reply first',
          children: jsx(Button, {
            size: 'xs',
            variant: 'secondary',
            disabled: !reply.trim(),
            onClick: async () => {
              if (await action('more_work', reply)) sendToAgent({ ...project, pins: [{ ...pin, status: 'open' }] }, [pin.number])
            },
            children: answering ? 'Send reply' : 'Needs more work'
          })
        },
        'more'
      )
    )
  }
  if (pin.result_version && !st.closed && !st.busy)
    buttons.push(
      jsx(
        Button,
        {
          size: 'xs',
          variant: 'ghost',
          onClick: () => act(() => rest(`/projects/${project.id}/revert`, { method: 'POST', body: { pin: pin.number } })),
          children: 'Revert'
        },
        'rev'
      )
    )
  if (st.closed) buttons.push(jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => action('reopen'), children: 'Reopen' }, 'reo'))
  else if (pin.status !== 'open' && !st.busy)
    buttons.push(jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => action('wont_fix'), children: "Won't fix" }, 'wf'))
  return buttons
}

function PinBubble({ project, pin, onClose }) {
  const [reply, setReply] = useState('')
  const st = statusOf(pin.status)
  const reviewing = pin.status === 'needs_review' || pin.status === 'question'
  const spill = (project.versions.find(v => v.id === pin.result_version)?.spill || '').split(',').filter(Boolean)
  const action = async (name, message = '') => {
    const out = await act(() => rest(`/projects/${project.id}/pins/${pin.number}/action`, { method: 'POST', body: { action: name, message } }))
    if (out) setReply('')
    return out
  }
  const buttons = pinButtons({ project, pin, reply, action, onClose })

  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 8 },
    children: [
      jsxs('div', {
        style: { display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 },
        children: [
          jsx('span', { style: { fontWeight: 700, color: 'var(--ui-text-primary)' }, children: `#${pin.number}` }),
          jsx('span', { style: { color: 'var(--ui-text-tertiary)', textTransform: 'capitalize' }, children: pin.intent }),
          pin.round > 1 && jsx('span', { style: { color: 'var(--ui-text-quaternary)' }, children: `· round ${pin.round}` }),
          jsx('span', { style: { flex: 1 } }),
          jsxs('span', {
            style: { display: 'inline-flex', alignItems: 'center', gap: 5, color: 'var(--ui-text-secondary)' },
            children: [
              st.busy
                ? jsx(GlyphSpinner, { ariaLabel: 'Working' })
                : jsx('span', { style: { width: 7, height: 7, borderRadius: 99, background: st.color } }),
              st.label
            ]
          }),
          jsx(Button, { size: 'icon-xs', variant: 'ghost', 'aria-label': 'Close', onClick: onClose, children: icon(icons.X, 12) })
        ]
      }),
      jsx('div', { style: { fontSize: 13, lineHeight: '18px', color: 'var(--ui-text-primary)', whiteSpace: 'pre-wrap' }, children: pin.note }),
      pin.thread.length > 0 &&
        jsx('div', {
          style: { display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 200, overflowY: 'auto' },
          children: pin.thread.map((m, i) => jsx(Message, { m }, i))
        }),
      spill.length > 0 &&
        !st.closed &&
        jsx('div', {
          style: WARN,
          children: `Edge check: what was removed continues past this mark (${spill.join(', ')}). That part stays. Draw a bigger mark to remove it.`
        }),
      st.busy && jsx('div', { style: { fontSize: 12, color: 'var(--ui-text-tertiary)' }, children: 'Hermes is working on this mark…' }),
      reviewing &&
        jsx(Textarea, {
          rows: 2,
          placeholder: pin.status === 'question' ? 'Answer Hermes…' : 'Reply, or say what is still wrong…',
          value: reply,
          onChange: e => setReply(e.target.value)
        }),
      buttons.length > 0 && jsx('div', { style: { display: 'flex', flexWrap: 'wrap', justifyContent: 'flex-end', gap: 4 }, children: buttons })
    ]
  })
}

function BubbleAnchor({ project, draft, selected, setDraft, onSelect, paneRef }) {
  const W = project.width
  const H = project.height
  const pin = selected ? project.pins.find(p => p.number === selected) : null
  const target = draft?.ready ? { shape: draft.shape, gp: draft.geom } : pin ? { shape: pin.shape, gp: toPx(pin.shape, pin.geom, W, H) } : null
  if (!target) return null
  const b = bounds(target.shape, target.gp)
  const close = () => {
    setDraft(null)
    onSelect(null)
  }
  return jsxs(
    Popover,
    {
      open: true,
      onOpenChange: open => !open && close(),
      children: [
        jsx(PopoverTrigger, {
          asChild: true,
          children: jsx('span', {
            'aria-hidden': true,
            style: { position: 'absolute', left: pct(b.x, W), top: pct(b.y, H), width: pct(b.w, W), height: pct(b.h, H), pointerEvents: 'none' }
          })
        }),
        jsx(PopoverContent, {
          side: 'right',
          align: 'start',
          sideOffset: 12,
          collisionPadding: 12,
          style: { width: 300, padding: 12 },
          // The pane handles its own clicks and focus (drawing, selecting, the toolbar); only
          // interaction elsewhere dismisses. Pointer-down alone is not enough: the click then focuses
          // the badge or tool button, and Radix would dismiss on that focus move.
          onInteractOutside: e => paneRef.current?.contains(e.target) && e.preventDefault(),
          onOpenAutoFocus: e => {
            e.preventDefault()
            if (draft?.ready) requestAnimationFrame(() => document.querySelector('[data-retouch-note]')?.focus())
          },
          children: draft?.ready
            ? jsx(DraftBubble, {
                project,
                draft,
                onCancel: close,
                onAdded: n => {
                  setDraft(null)
                  onSelect(n)
                }
              })
            : jsx(PinBubble, { project, pin, onClose: close })
        })
      ]
    },
    draft?.ready ? 'draft' : `pin-${selected}`
  )
}

// --- the stage: image, marks, drawing ----------------------------------------------------------

function Stage({ project, image, comparing, tool, shapeKind, draft, setDraft, selected, onSelect }) {
  const svg = useRef(null)
  const gesture = useRef(null)
  const [drag, setDrag] = useState(null)
  const W = project.width
  const H = project.height
  const S = Math.min(W, H)

  const toImage = e => {
    const pt = svg.current.createSVGPoint()
    pt.x = e.clientX
    pt.y = e.clientY
    const p = pt.matrixTransform(svg.current.getScreenCTM().inverse())
    return [Math.max(0, Math.min(W, p.x)), Math.max(0, Math.min(H, p.y))]
  }
  const perScreenPx = () => W / svg.current.getBoundingClientRect().width

  const down = e => {
    if (e.button !== 0 || comparing) return
    const p = toImage(e)
    const hitPin = Number(e.target.getAttribute?.('data-pin')) || null
    if (tool === 'select') {
      onSelect(hitPin)
      const pin = hitPin && project.pins.find(x => x.number === hitPin)
      if (pin?.status === 'open') {
        e.currentTarget.setPointerCapture(e.pointerId)
        gesture.current = { kind: 'move', pin, start: p }
      }
      return
    }
    e.currentTarget.setPointerCapture(e.pointerId)
    onSelect(null)
    if (tool === 'text') {
      const r = NOTE_R * S
      const x = Math.max(0, Math.min(W - 2 * r, p[0] - r))
      const y = Math.max(0, Math.min(H - 2 * r, p[1] - r))
      setDraft({ shape: 'ellipse', geom: { x, y, w: 2 * r, h: 2 * r }, intent: 'ask', ready: true })
      return
    }
    if (tool === 'pen') {
      gesture.current = { kind: 'path', points: [p] }
      setDraft({ shape: 'brush', geom: { points: [p], width: BRUSH * S } })
    } else if (shapeKind === 'lasso') {
      gesture.current = { kind: 'path', points: [p] }
      setDraft({ shape: 'lasso', geom: { points: [p] } })
    } else {
      gesture.current = { kind: 'box', start: p }
      setDraft({ shape: shapeKind, geom: boxFrom(p, p) })
    }
  }

  const move = e => {
    const g = gesture.current
    if (!g) return
    const p = toImage(e)
    if (g.kind === 'move') setDrag({ number: g.pin.number, dx: p[0] - g.start[0], dy: p[1] - g.start[1] })
    else if (g.kind === 'box') setDraft(d => d && { ...d, geom: boxFrom(g.start, p) })
    else {
      const last = g.points[g.points.length - 1]
      if (Math.hypot(p[0] - last[0], p[1] - last[1]) < 2.5 * perScreenPx()) return
      g.points.push(p)
      setDraft(d => d && { ...d, geom: { ...d.geom, points: [...g.points] } })
    }
  }

  const up = async e => {
    const g = gesture.current
    gesture.current = null
    if (!g) return
    if (g.kind === 'move') {
      const p = toImage(e)
      const dx = p[0] - g.start[0]
      const dy = p[1] - g.start[1]
      if (Math.hypot(dx, dy) >= 3 * perScreenPx()) {
        const geom = translate(g.pin.shape, toPx(g.pin.shape, g.pin.geom, W, H), dx, dy, W, H)
        await act(() => rest(`/projects/${project.id}/pins/${g.pin.number}`, { method: 'PATCH', body: { geom } }))
      }
      setDrag(null)
      return
    }
    const min = MIN_MARK * Math.max(W, H)
    setDraft(d => {
      if (!d) return d
      if (d.shape === 'lasso' && d.geom.points.length < 3) return null
      const b = bounds(d.shape, d.geom)
      if (d.shape !== 'brush' && (b.w < min || b.h < min)) return null
      return { ...d, ready: true }
    })
  }

  const shown = project.pins.map(p => {
    let gp = toPx(p.shape, p.geom, W, H)
    if (drag?.number === p.number) gp = translate(p.shape, gp, drag.dx, drag.dy, W, H)
    return { pin: p, gp, at: badgeAt(p.shape, gp) }
  })

  return jsxs('div', {
    style: { position: 'relative', aspectRatio: `${W} / ${H}`, width: `min(100cqw, calc(100cqh * ${W / H}))` },
    children: [
      image
        ? jsx('img', {
            src: image.data_url,
            alt: project.title,
            draggable: false,
            style: { position: 'absolute', inset: 0, width: '100%', height: '100%', userSelect: 'none', borderRadius: 2 }
          })
        : jsx('div', { style: { position: 'absolute', inset: 0, background: 'var(--ui-bg-quaternary)', borderRadius: 2 } }),
      jsxs('svg', {
        ref: svg,
        viewBox: `0 0 ${W} ${H}`,
        preserveAspectRatio: 'none',
        style: {
          position: 'absolute',
          inset: 0,
          width: '100%',
          height: '100%',
          touchAction: 'none',
          cursor: comparing || tool === 'select' ? 'default' : 'crosshair',
          opacity: comparing ? 0 : 1
        },
        onPointerDown: down,
        onPointerMove: move,
        onPointerUp: up,
        onPointerCancel: () => {
          gesture.current = null
          setDrag(null)
        },
        children: [
          ...shown.map(({ pin, gp }) => {
            const st = statusOf(pin.status)
            return jsx(
              Shape,
              {
                shape: pin.shape,
                gp,
                pin: pin.number,
                color: st.color,
                selected: selected === pin.number,
                busy: st.busy,
                faded: st.closed && selected !== pin.number,
                hit: tool === 'select'
              },
              pin.number
            )
          }),
          draft && jsx(Shape, { shape: draft.shape, gp: draft.geom, color: 'var(--ui-accent)', dashed: true, selected: true }, 'draft')
        ]
      }),
      // Number badges live in HTML so their text stays crisp at any image size.
      jsx('div', {
        style: { position: 'absolute', inset: 0, pointerEvents: 'none', opacity: comparing ? 0 : 1 },
        children: shown.map(({ pin, at }) => {
          const st = statusOf(pin.status)
          const quiet = st.closed && selected !== pin.number
          return jsx(
            'button',
            {
              type: 'button',
              'aria-label': `Mark ${pin.number}: ${st.label}`,
              onPointerDown: e => {
                e.stopPropagation()
                onSelect(pin.number)
              },
              style: {
                position: 'absolute',
                left: pct(at[0], W),
                top: pct(at[1], H),
                transform: 'translate(-50%, -50%)',
                minWidth: 22,
                height: 22,
                padding: '0 6px',
                borderRadius: 99,
                border: '2px solid var(--ui-bg-editor)',
                background: quiet ? 'var(--ui-bg-quaternary)' : st.color,
                color: quiet ? 'var(--ui-text-tertiary)' : 'var(--dt-primary-solid-foreground, var(--ui-bg-editor))',
                fontSize: 11,
                fontWeight: 700,
                lineHeight: '18px',
                cursor: 'pointer',
                pointerEvents: comparing ? 'none' : 'auto',
                boxShadow: selected === pin.number ? `0 0 0 3px color-mix(in srgb, ${st.color} 35%, transparent)` : 'none'
              },
              children: pin.status === 'resolved' ? '✓' : String(pin.number)
            },
            pin.number
          )
        })
      })
    ]
  })
}

// --- chrome: header and floating toolbar -------------------------------------------------------

function IconButton({ label, active = false, children, ...props }) {
  return jsx(Tip, {
    label,
    children: jsx(Button, { size: 'icon-sm', variant: active ? 'secondary' : 'ghost', 'aria-label': label, 'aria-pressed': active, ...props, children })
  })
}

function Header({ project, comparing, setComparing, onOpen, onOpened, onDiscarded }) {
  const current = project.versions.find(v => v.id === project.current_version)
  const edits = current ? current.seq : 0
  const working = project.pins.some(p => statusOf(p.status).busy)
  const release = () => setComparing(false)
  const canShoot = useCanScreenshot()
  const [shooting, setShooting] = useState(false)
  return jsxs('div', {
    style: {
      height: 40,
      flexShrink: 0,
      display: 'flex',
      alignItems: 'center',
      gap: 2,
      padding: '0 6px 0 12px',
      borderBottom: '1px solid var(--ui-stroke-tertiary)',
      fontSize: 13
    },
    children: [
      jsx('span', {
        style: { fontWeight: 600, color: 'var(--ui-text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 },
        children: project.title
      }),
      jsx('span', {
        style: { marginLeft: 8, fontSize: 11, color: 'var(--ui-text-quaternary)', whiteSpace: 'nowrap' },
        children: comparing ? 'Showing the original' : edits ? `${edits} edit${edits > 1 ? 's' : ''}` : 'Original'
      }),
      working &&
        jsxs('span', {
          style: { display: 'inline-flex', alignItems: 'center', gap: 5, marginLeft: 10, fontSize: 11, color: 'var(--ui-text-tertiary)', whiteSpace: 'nowrap' },
          children: [jsx(GlyphSpinner, { ariaLabel: 'Working' }), 'Hermes is working']
        }),
      jsx('span', { style: { flex: 1 } }),
      project.scratch &&
        jsxs('span', {
          style: { display: 'inline-flex', alignItems: 'center', gap: 4, marginRight: 6 },
          children: [
            jsx('span', { style: { fontSize: 11, color: 'var(--ui-text-quaternary)', whiteSpace: 'nowrap' }, children: 'Screenshot · kept once you mark, edit or save it' }),
            jsxs(Button, {
              size: 'sm',
              variant: 'ghost',
              onClick: () => act(() => rest(`/projects/${project.id}/keep`, { method: 'POST', body: {} })),
              children: [icon(icons.Check), 'Keep']
            }),
            jsxs(Button, {
              size: 'sm',
              variant: 'ghost',
              onClick: async () => {
                const ok = await act(() => rest(`/projects/${project.id}`, { method: 'DELETE' }))
                if (ok) onDiscarded(project.id)
              },
              children: [icon(icons.Trash2), 'Discard']
            })
          ]
        }),
      jsx(IconButton, {
        label: 'Hold to see the original',
        active: comparing,
        disabled: !edits,
        onPointerDown: () => setComparing(true),
        onPointerUp: release,
        onPointerLeave: release,
        onPointerCancel: release,
        children: icon(icons.Eye)
      }),
      jsx(IconButton, {
        label: 'Undo the last edit',
        disabled: !edits,
        onClick: () => act(() => rest(`/projects/${project.id}/revert`, { method: 'POST', body: {} })),
        children: jsx(UndoGlyph, {})
      }),
      jsxs(DropdownMenu, {
        children: [
          jsx(DropdownMenuTrigger, {
            asChild: true,
            children: jsxs(Button, { size: 'sm', variant: 'ghost', children: [icon(icons.Download), 'Save', icon(icons.ChevronDown, 12)] })
          }),
          jsx(DropdownMenuContent, {
            align: 'end',
            children: FORMATS.map(([format, label, hint]) =>
              jsxs(
                DropdownMenuItem,
                {
                  onSelect: () => exportImage(project, format),
                  children: [
                    jsx('span', { style: { flex: 1 }, children: `Save as ${label}` }),
                    jsx('span', { style: { fontSize: 11, color: 'var(--ui-text-quaternary)' }, children: hint })
                  ]
                },
                format
              )
            )
          })
        ]
      }),
      canShoot &&
        jsx(IconButton, {
          label: shooting ? 'Pick a region…' : 'Screenshot a new image',
          disabled: shooting,
          onClick: async () => {
            setShooting(true)
            try {
              await takeScreenshot(onOpened)
            } finally {
              setShooting(false)
            }
          },
          children: icon(icons.Monitor)
        }),
      jsx(IconButton, { label: 'Open another image', onClick: onOpen, children: icon(icons.FolderOpen) }),
      closeTile && jsx(IconButton, { label: 'Close Retouch', onClick: () => closeTile?.(), children: icon(icons.X) })
    ]
  })
}

function ShapePicker({ tool, setTool, shapeKind, setShapeKind }) {
  const current = SHAPES[shapeKind]
  return jsxs('span', {
    style: { display: 'inline-flex', alignItems: 'center' },
    children: [
      jsx(IconButton, {
        label: `${current.label} (${current.key.toUpperCase()})`,
        active: tool === 'shape',
        onClick: () => setTool('shape'),
        children: jsx(current.Glyph, {})
      }),
      jsxs(DropdownMenu, {
        children: [
          jsx(DropdownMenuTrigger, {
            asChild: true,
            children: jsx(Button, {
              size: 'icon-xs',
              variant: 'ghost',
              'aria-label': 'Choose a shape',
              style: { width: 18, marginLeft: -3, color: 'var(--ui-text-tertiary)' },
              children: icon(icons.ChevronDown, 14)
            })
          }),
          jsx(DropdownMenuContent, {
            align: 'center',
            side: 'top',
            children: Object.entries(SHAPES).map(([kind, s]) =>
              jsxs(
                DropdownMenuItem,
                {
                  onSelect: () => {
                    setShapeKind(kind)
                    setTool('shape')
                  },
                  children: [
                    jsx(s.Glyph, {}),
                    jsx('span', { style: { flex: 1 }, children: s.label }),
                    jsx('span', { style: { fontSize: 11, color: 'var(--ui-text-quaternary)' }, children: s.key.toUpperCase() })
                  ]
                },
                kind
              )
            )
          })
        ]
      })
    ]
  })
}

function Toolbar({ project, tool, setTool, shapeKind, setShapeKind, onClose }) {
  const open = project.pins.filter(p => p.status === 'open').length
  return jsxs('div', {
    className: 'shadow-md',
    style: {
      position: 'absolute',
      left: '50%',
      bottom: 14,
      transform: 'translateX(-50%)',
      display: 'flex',
      alignItems: 'center',
      gap: 2,
      padding: 4,
      borderRadius: 12,
      border: '1px solid var(--ui-stroke-secondary)',
      background: 'color-mix(in srgb, var(--ui-bg-elevated) 94%, transparent)',
      backdropFilter: 'blur(12px)',
      zIndex: 2
    },
    children: [
      jsx(IconButton, { label: 'Select and move (V)', active: tool === 'select', onClick: () => setTool('select'), children: jsx(PointerGlyph, {}) }),
      jsx(IconButton, { label: 'Pen (P)', active: tool === 'pen', onClick: () => setTool('pen'), children: icon(icons.Pencil) }),
      jsx(ShapePicker, { tool, setTool, shapeKind, setShapeKind }),
      jsx(IconButton, { label: 'Note at a spot (T)', active: tool === 'text', onClick: () => setTool('text'), children: jsx(TextGlyph, {}) }),
      jsx('span', { style: { width: 1, height: 20, margin: '0 4px', background: 'var(--ui-stroke-secondary)' } }),
      open > 0 &&
        jsx(Tip, {
          label: 'Ask Hermes in the chat to work on the open marks',
          children: jsxs(Button, {
            size: 'sm',
            onClick: () => sendToAgent(project),
            style: { marginRight: 2 },
            children: [icon(icons.Send, 14), `Send ${open}`]
          })
        }),
      jsx(IconButton, { label: 'Close this image', onClick: onClose, children: icon(icons.X) })
    ]
  })
}

// --- the workbench -----------------------------------------------------------------------------

const TOOL_KEYS = { v: ['select'], p: ['pen'], t: ['text'], r: ['shape', 'rect'], e: ['shape', 'ellipse'], l: ['shape', 'lasso'] }

function LoadError({ error, retry, onClose }) {
  return jsxs('div', {
    style: { height: '100%', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 10, padding: 24, fontSize: 13 },
    children: [
      jsx('div', { style: { fontWeight: 600, color: 'var(--ui-text-primary)' }, children: 'Could not open this image' }),
      jsx('div', { style: { color: 'var(--ui-text-tertiary)', textAlign: 'center', maxWidth: 340 }, children: errorText(error) }),
      jsxs('div', {
        style: { display: 'flex', gap: 6 },
        children: [
          jsx(Button, { size: 'sm', variant: 'secondary', onClick: () => retry(), children: 'Try again' }),
          jsx(Button, { size: 'sm', variant: 'ghost', onClick: onClose, children: 'Back' })
        ]
      })
    ]
  })
}

function Workbench({ projectId, onOpened, onClose }) {
  const { data: project, error, refetch } = useProject(projectId)
  const [comparing, setComparing] = useState(false)
  const { data: current } = useImage(projectId, project?.current_version)
  const { data: before } = useImage(projectId, project?.versions?.[0]?.id, comparing)
  const [tool, setTool] = useState('shape')
  const [shapeKind, setShapeKind] = useState('ellipse')
  const [draft, setDraft] = useState(null)
  const [selected, setSelected] = useState(null)
  const pane = useRef(null)
  const input = useRef(null)

  useEffect(() => {
    if (selected && project && !project.pins.some(p => p.number === selected)) setSelected(null)
  }, [project, selected])

  if (!project && error) return jsx(LoadError, { error, retry: refetch, onClose })
  if (!project) return jsx('div', { style: { padding: 16, fontSize: 13, color: 'var(--ui-text-tertiary)' }, children: 'Loading…' })

  const onKeyDown = e => {
    if (e.metaKey || e.ctrlKey || e.altKey || e.target.closest?.(TYPING)) return
    const key = e.key.toLowerCase()
    if (TOOL_KEYS[key]) {
      const [t, kind] = TOOL_KEYS[key]
      setTool(t)
      if (kind) setShapeKind(kind)
      e.preventDefault()
    } else if (key === 'escape') {
      setDraft(null)
      setSelected(null)
    } else if ((key === 'delete' || key === 'backspace') && selected) {
      const pin = project.pins.find(p => p.number === selected)
      if (pin?.status !== 'open') return
      e.preventDefault()
      act(() => rest(`/projects/${project.id}/pins/${pin.number}`, { method: 'DELETE' })).then(() => setSelected(null))
    }
  }
  const select = n => {
    setSelected(n)
    if (n) setDraft(null)
  }
  const chooseTool = t => {
    setTool(t)
    if (draft && !draft.ready) setDraft(null)
  }

  return jsxs('div', {
    ref: pane,
    tabIndex: 0,
    onKeyDown,
    onPointerDown: e => !e.target.closest?.(`${TYPING}, button, [role="menuitem"]`) && pane.current?.focus({ preventScroll: true }),
    onDragOver: e => e.preventDefault(),
    onDrop: e => {
      e.preventDefault()
      upload(imageFrom(e.dataTransfer), onOpened)
    },
    onPaste: e => {
      if (!e.target.closest?.(TYPING)) upload(imageFrom(e.clipboardData), onOpened)
    },
    style: { position: 'relative', height: '100%', display: 'flex', flexDirection: 'column', outline: 'none', overflow: 'hidden' },
    children: [
      jsx(FilePicker, { inputRef: input, onOpened }),
      jsx(Header, { project, comparing, setComparing, onOpen: () => input.current?.click(), onOpened, onDiscarded: onClose }),
      jsxs('div', {
        style: { position: 'relative', flex: 1, minHeight: 0 },
        children: [
          jsx('div', {
            style: { position: 'absolute', inset: '14px 14px 70px 14px', containerType: 'size', display: 'flex', alignItems: 'center', justifyContent: 'center' },
            children: jsxs('div', {
              style: { position: 'relative' },
              children: [
                jsx(Stage, { project, image: comparing && before ? before : current, comparing, tool, shapeKind, draft, setDraft, selected, onSelect: select }),
                !comparing &&
                  jsx('div', {
                    style: { position: 'absolute', inset: 0, pointerEvents: 'none' },
                    children: jsx(BubbleAnchor, { project, draft, selected, setDraft, onSelect: setSelected, paneRef: pane })
                  })
              ]
            })
          }),
          !draft &&
            project.pins.length === 0 &&
            jsx('div', {
              style: {
                position: 'absolute',
                left: '50%',
                bottom: 64,
                transform: 'translateX(-50%)',
                padding: '4px 10px',
                borderRadius: 99,
                fontSize: 12,
                whiteSpace: 'nowrap',
                color: 'var(--ui-text-secondary)',
                background: 'color-mix(in srgb, var(--ui-bg-elevated) 90%, transparent)',
                border: '1px solid var(--ui-stroke-tertiary)',
                pointerEvents: 'none'
              },
              children: 'Draw on the image where something needs work'
            }),
          jsx(Toolbar, { project, tool, setTool: chooseTool, shapeKind, setShapeKind, onClose })
        ]
      })
    ]
  })
}

function RetouchPane() {
  const [projectId, setProjectId] = useState(() => api.storage.get('project') || null)
  const choose = next => {
    if (next) api.storage.set('project', next)
    else api.storage.remove('project')
    setProjectId(next)
  }
  return projectId
    ? jsx(Workbench, {
        projectId,
        onOpened: choose,
        onClose: gone => {
          // a discarded project must not be refetched by the queries still mounted for it
          if (gone) queryClient.removeQueries({ queryKey: [...QK, 'project', gone] })
          choose(null)
        }
      }, projectId)
    : jsx(Start, { onOpened: choose })
}

// Retouch is a window you open and close, not a fixture: enabling the plugin adds one toggle
// (⌘⇧E, the image button in the title bar, or "Retouch" in ⌘K). Open puts the workbench in a
// closeable tile docked to the RIGHT of the chat, like a session tile, so you mark while you talk;
// the same toggle, the tile's tab ×, ⌘W or the × in Retouch's own header closes it. Closing never
// disables the plugin.
let closeTile = null // disposer of the open tile, while one is open
const TILE_PANE_ID = 'plugin-workspace:retouch' // what openWorkspace names the tile

function openRetouch() {
  if (typeof host.openWorkspace !== 'function') {
    host.notify({ kind: 'error', message: 'Retouch needs Hermes Desktop 0.22 or newer to open its window.' })
    return
  }
  // Re-opening the same id fronts the existing tile instead of stacking another, so a tile the
  // user tucked behind the chat comes forward rather than closing.
  closeTile = host.openWorkspace('retouch', {
    title: 'Retouch',
    dock: { pane: 'workspace', pos: 'right' },
    minWidth: '26rem',
    render: () => jsx(RetouchPane, {}),
    onClose: () => {
      closeTile = null
    }
  })
}

function toggleRetouch() {
  const visible = typeof host.paneVisibility === 'function' ? host.paneVisibility(TILE_PANE_ID).get() : true
  if (closeTile && visible) closeTile()
  else openRetouch()
}

export default {
  id: 'retouch',
  name: 'Retouch',
  register(ctx) {
    api = ctx
    ctx.onEvent(EVENT, () => refresh())
    ctx.onDispose(() => closeTile?.())
    ctx.registerMany([
      {
        id: 'toggle',
        area: PALETTE_AREA,
        data: { id: 'retouch.toggle', label: 'Retouch: open / close', action: 'retouch.toggle', keywords: ['retouch', 'image', 'edit', 'mark', 'screenshot'], run: toggleRetouch }
      },
      {
        id: 'key-toggle',
        area: KEYBINDS_AREA,
        data: { id: 'retouch.toggle', label: 'Open or close Retouch', category: 'view', defaults: ['mod+shift+e'], run: toggleRetouch }
      },
      {
        id: 'titlebar',
        area: TITLEBAR_AREAS.right,
        data: { id: 'retouch.titlebar', label: 'Retouch', icon: icon(icons.ImageIcon), actionId: 'retouch.toggle', onSelect: toggleRetouch }
      }
    ])
    // Enabling the plugin shows nothing by itself; say once where the window is.
    if (!ctx.storage.get('introduced', false)) {
      ctx.storage.set('introduced', true)
      host.notify({ kind: 'info', message: 'Retouch is on. Open it with ⌘⇧E or the image button in the title bar.' })
    }
  }
}
