// Passive CDP observer. Never creates a platform socket or sends MQTT packets.
import { randomUUID } from 'node:crypto';
const LIMIT = 2 * 1024 * 1024;
function vint(b, p) {
  let n = 0n;
  for (let i = 0; i < 10; i++) {
    if (p >= b.length) throw Error('truncated protobuf');
    const c = b[p++]; n |= BigInt(c & 127) << BigInt(i * 7);
    if (!(c & 128)) return [n, p];
  }
  throw Error('invalid varint');
}
function fields(b) {
  const out = new Map(); let p = 0;
  while (p < b.length) {
    let tag; [tag, p] = vint(b, p);
    const key = Number(tag >> 3n), wire = Number(tag & 7n); let v;
    if (!key) throw Error('invalid field');
    if (wire === 0) [v, p] = vint(b, p);
    else if ([1, 2, 5].includes(wire)) {
      let size = wire === 1 ? 8 : 4;
      if (wire === 2) { let n; [n, p] = vint(b, p); size = Number(n); }
      if (size > LIMIT || p + size > b.length) throw Error('truncated field');
      v = b.subarray(p, p + size); p += size;
    } else throw Error('unsupported wire');
    if (!out.has(key)) out.set(key, []);
    out.get(key).push(v);
  }
  return out;
}
const first = (f, n) => f.get(n)?.[0];
const integer = (v) => typeof v === 'bigint' ? String(v) : null;
function user(b, requireSource = false) {
  if (!Buffer.isBuffer(b)) throw Error('missing user');
  const f = fields(b), uid = integer(first(f, 1)), source = integer(first(f, 7));
  if (!uid || uid === '0') throw Error('missing uid');
  if (requireSource && !['0', '1'].includes(source)) throw Error('unknown candidate source');
  return { uid, source }; // Source must be explicit to match a bound candidate.
}
export function protocolEvents(b) {
  const f = fields(b), kind = integer(first(f, 1));
  if (!['1', '2', '3', '4', '5', '6', '7', '8', '9'].includes(kind)) throw Error('unknown chat protocol');
  if (kind === '1' && !f.has(3)) throw Error('unknown message envelope');
  return (f.get(3) || []).map(raw => {
    const m = fields(raw), mid = integer(first(m, 4));
    if (!mid || mid === '0') throw Error('missing message id');
    const from = user(first(m, 1)), to = user(first(m, 2));
    // Browser-authored messages can omit the account's source. The candidate
    // endpoint must still have an explicit source; Python checks account UID.
    if (![from.source, to.source].some(source => ['0', '1'].includes(source))) throw Error('unknown candidate source');
    return { mid, from, to };
  });
}
export function chatURL(value) {
  try { const u = new URL(value); return u.protocol === 'wss:' && (u.hostname === 'zhipin.com' || u.hostname.endsWith('.zhipin.com')) && u.pathname === '/chatws'; }
  catch { return false; }
}
export function securityURL(value) {
  try { const u = new URL(value); return (u.hostname === 'zhipin.com' || u.hostname.endsWith('.zhipin.com')) &&
    (['/web/passport/zp/verify.html', '/web/passport/zp/403.html'].includes(u.pathname) || (u.pathname === '/' && u.searchParams.has('_security_check'))); }
  catch { return false; }
}
export class ChatObserver {
  constructor() { this.epoch = randomUUID(); this.states = new Map(); }
  state(session) {
    if (!this.states.has(session)) this.states.set(session, { id: randomUUID(), sockets: new Map(), seq: 0, generation: 0, events: [], paused: false, reason: '尚未捕获可确认的 BOSS WS 连接；继续 HTTP 轮询，可手动刷新 BOSS 沟通页建立监听' });
    return this.states.get(session);
  }
  reset() { this.states.clear(); this.epoch = randomUUID(); }
  remove(session) { this.states.delete(session); }
  handle(msg, now = Date.now()) {
    if (!msg.sessionId || !this.states.has(msg.sessionId)) return;
    const s = this.state(msg.sessionId), p = msg.params || {};
    if (msg.method === 'Page.frameNavigated' && !p.frame?.parentId) {
      s.sockets.clear(); s.generation++; s.paused = securityURL(p.frame?.url);
    }
    if (msg.method === 'Network.responseReceived') {
      const r = p.response || {}; let u;
      try { u = new URL(r.url); } catch { return; }
      if (securityURL(r.url) || ((u.hostname === 'zhipin.com' || u.hostname.endsWith('.zhipin.com')) &&
        [403, 429].includes(r.status) && (p.type === 'Document' || u.pathname.startsWith('/wapi/')))) s.paused = true;
    }
    if (msg.method === 'Network.requestWillBeSent' && (securityURL(p.request?.url) || securityURL(p.redirectResponse?.url))) s.paused = true;
    if (msg.method === 'Network.webSocketCreated' && chatURL(p.url)) {
      s.sockets.clear(); s.generation++;
      s.reason = '已捕获 BOSS WS 连接，等待数据确认；使用 HTTP 轮询';
      s.sockets.set(p.requestId, { buffer: Buffer.alloc(0), sentBuffer: Buffer.alloc(0), last: 0, failed: false });
    }
    const socket = s.sockets.get(p.requestId);
    if (!socket) return;
    if (['Network.webSocketClosed', 'Network.webSocketFrameError'].includes(msg.method)) {
      s.sockets.delete(p.requestId); s.reason = 'BOSS WS 已断开，使用 HTTP 轮询'; return;
    }
    const sent = msg.method === 'Network.webSocketFrameSent';
    if ((!sent && msg.method !== 'Network.webSocketFrameReceived') || socket.failed) return;
    const opcode = p.response?.opcode;
    // WebSocket controls are not MQTT application data and may interleave
    // fragmented packets. Only received MQTT packets establish server health.
    if (opcode === 9 || opcode === 10) return;
    if (opcode === 8) {
      s.sockets.delete(p.requestId);
      s.reason = 'BOSS WS 已关闭，使用 HTTP 轮询';
      return;
    }
    const bufferKey = sent ? 'sentBuffer' : 'buffer';
    try {
      if (opcode !== 2) throw Error('unknown frame');
      if ((p.response.payloadData || '').length > LIMIT * 2) throw Error('frame too large');
      socket[bufferKey] = Buffer.concat([socket[bufferKey], Buffer.from(p.response.payloadData, 'base64')]);
      if (socket[bufferKey].length > LIMIT) throw Error('packet too large');
      while (socket[bufferKey].length >= 2) {
        const b = socket[bufferKey], type = b[0] >> 4; let size = 0, pos = 1, complete = false;
        for (let i = 0; i < 4; i++) {
          if (pos >= b.length) return;
          const c = b[pos++]; size += (c & 127) * (128 ** i);
          if (!(c & 128)) { complete = true; break; }
        }
        if (!complete || size > LIMIT) throw Error('invalid MQTT size');
        const end = pos + size;
        if (end > b.length) return;
        if (type === 3) {
          if (pos + 2 > end) throw Error('missing topic');
          const n = b.readUInt16BE(pos); pos += 2;
          if (pos + n > end) throw Error('truncated topic');
          const topic = b.subarray(pos, pos + n).toString(); pos += n;
          const qos = (b[0] >> 1) & 3;
          if (qos === 3) throw Error('invalid qos');
          if (qos) pos += 2;
          if (pos > end || topic !== 'chat') throw Error('unknown topic');
          for (const event of protocolEvents(b.subarray(pos, end))) {
            s.events.push({ seq: ++s.seq, ...event });
            if (s.events.length > 1024) s.events.shift();
          }
        } else if (!(sent
          ? ((type === 1 && size >= 10) || (type === 8 && size >= 5) || (type === 10 && size >= 5) || (type === 4 && size === 2) || ([12, 14].includes(type) && size === 0))
          : ((type === 2 && size === 2 && b[pos + 1] === 0) || (type === 4 && size === 2) || (type === 13 && size === 0)))) {
          throw Error('unknown MQTT packet');
        }
        if (!sent) socket.last = now; // Sending alone cannot prove a live server.
        socket[bufferKey] = b.subarray(end);
      }
    } catch {
      socket.failed = true; socket.buffer = Buffer.alloc(0); s.reason = 'WS 数据格式无法确认，已退回 HTTP 轮询';
    }
  }
  snapshot(session, after = 0, now = Date.now()) {
    const s = this.state(session);
    const healthy = [...s.sockets.values()].some(x => !x.failed && x.last > 0 && now - x.last < 90000);
    const stale = [...s.sockets.values()].some(x => !x.failed && x.last > 0 && now - x.last >= 90000);
    return { epoch: `${this.epoch}:${s.id}`, generation: s.generation, cursor: s.seq, connected: healthy && !s.paused,
      paused: s.paused, gap: after > s.seq || (s.events.length > 0 && after < s.events[0].seq - 1),
      reason: s.paused ? '检测到 BOSS 验证或限制页面，需人工处理' : healthy ? 'BOSS WS 监听中' : stale ? 'BOSS WS 超过 90 秒未收到数据，使用 HTTP 轮询' : s.reason,
      events: s.events.filter(x => x.seq > after) };
  }
}
