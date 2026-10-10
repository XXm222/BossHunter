import test from 'node:test';
import assert from 'node:assert/strict';
import { ChatObserver, protocolEvents, chatURL } from '../../src/bosshunter/browser/runtime/chat-observer.mjs';
const vi = value => { let n = BigInt(value), a = []; do { let c = Number(n & 127n); n >>= 7n; a.push(c | (n ? 128 : 0)); } while (n); return Buffer.from(a); };
const num = (f, n) => Buffer.concat([vi(f * 8), vi(n)]);
const bytes = (f, b) => Buffer.concat([vi(f * 8 + 2), vi(b.length), b]);
const usr = (id, source = 0) => Buffer.concat([num(1, id), num(7, source)]);
const message = Buffer.concat([bytes(1, usr('9007199254740993')), bytes(2, usr('9876543210000001')), num(4, '9007199254740997'), bytes(6, Buffer.from('ignored body'))]);
const proto = Buffer.concat([num(1, 1), bytes(3, message)]);
const pub = (body = proto, topic = 'chat') => { const t = Buffer.from(topic), p = Buffer.concat([Buffer.from([0, t.length]), t, Buffer.from([0, 1]), body]); return Buffer.concat([Buffer.from([0x32]), vi(p.length), p]); };
const created = (o, id = 'socket') => o.handle({sessionId:'s', method:'Network.webSocketCreated',params:{requestId:id,url:'wss://ws2.zhipin.com/chatws'}},1000);
const receive = (o, b, id = 'socket') => o.handle({sessionId:'s',method:'Network.webSocketFrameReceived',params:{requestId:id,response:{opcode:2,payloadData:b.toString('base64')}}},2000);
const send = (o, b) => o.handle({sessionId:'s',method:'Network.webSocketFrameSent',params:{requestId:'socket',response:{opcode:2,payloadData:b.toString('base64')}}},2000);
function observer() { const o = new ChatObserver(); o.state('s'); created(o); return o; }
test('large IDs stay exact and no message contents are exposed', () => {
  const result = protocolEvents(proto);
  assert.deepEqual(result,[{mid:'9007199254740997',from:{uid:'9007199254740993',source:'0'},to:{uid:'9876543210000001',source:'0'}}]);
  assert(!JSON.stringify(result).includes('body'));
});
test('URL validation includes server pool, excludes unrelated and insecure hosts', () => {
  assert(chatURL('wss://ws6.zhipin.com/chatws')); assert(!chatURL('ws://ws.zhipin.com/chatws')); assert(!chatURL('wss://zhipin.com.evil/chatws'));
});
test('unknown existing socket does not disable HTTP fallback', () => {
  const o = new ChatObserver(); o.state('s'); receive(o,pub()); assert.equal(o.snapshot('s').connected,false);
});
test('messages, receipt packets, cursor and timeout', () => {
  const o = observer(); receive(o,pub()); receive(o,Buffer.from([0x40,2,0,1]));
  assert(o.snapshot('s',0,3000).connected); assert.equal(o.snapshot('s',0,3000).events.length,1);
  assert.equal(o.snapshot('s',1,3000).events.length,0); assert(!o.snapshot('s',1,100000).connected);
});
test('fragmented and concatenated MQTT packets', () => {
  const o = observer(), p = pub(); receive(o,p.subarray(0,3)); assert(!o.snapshot('s',0,3000).connected);
  receive(o,Buffer.concat([p.subarray(3),Buffer.from([0xd0,0])])); assert.equal(o.snapshot('s',0,3000).events.length,1);
});
test('truncated protobuf, unknown topic/protocol fall back until new socket', () => {
  for (const packet of [pub(Buffer.from([0x1a,100])),pub(proto,'other'),pub(num(1,99))]) {
    const o = observer(); receive(o,packet); receive(o,Buffer.from([0xd0,0])); assert(!o.snapshot('s',0,3000).connected);
    created(o,'new'); receive(o,pub(),'new'); assert(o.snapshot('s',0,3000).connected);
  }
});
test('close, navigation, disconnect reset and isolation', () => {
  const o = observer(); receive(o,pub());
  o.handle({sessionId:'other',method:'Network.webSocketClosed',params:{requestId:'socket'}}); assert(o.snapshot('s',0,3000).connected);
  o.handle({sessionId:'s',method:'Network.webSocketClosed',params:{requestId:'socket'}}); assert(!o.snapshot('s',0,3000).connected);
  created(o); receive(o,pub()); o.handle({sessionId:'s',method:'Page.frameNavigated',params:{frame:{url:'https://www.zhipin.com/web/chat/index'}}}); assert(!o.snapshot('s').connected);
  const epoch = o.epoch; o.reset(); assert.notEqual(o.epoch,epoch);
});
test('restriction pages pause even with 304, redirects and API rejection', () => {
  for (const params of [
    {response:{url:'https://www.zhipin.com/web/passport/zp/403.html?code=32',status:304}},
    {response:{url:'https://www.zhipin.com/wapi/zpchat/test',status:429}}
  ]) {
    const o = observer(); o.handle({sessionId:'s',method:'Network.responseReceived',params}); assert(o.snapshot('s').paused);
  }
  const o = observer(); o.handle({sessionId:'s',method:'Network.requestWillBeSent',params:{request:{url:'https://www.zhipin.com/?_security_check=1'}}}); assert(o.snapshot('s').paused);
});
test('external failures and static 403 do not pause', () => {
  const o = observer(); for (const url of ['https://external.test/wapi/x','https://www.zhipin.com/a.png']) o.handle({sessionId:'s',method:'Network.responseReceived',params:{response:{url,status:403},type:'Image'}});
  assert(!o.snapshot('s').paused);
});
test('ring overflow reports gap and reconnect increments generation', () => {
  const o = observer(); for(let i=0;i<1026;i++) receive(o,pub());
  assert(o.snapshot('s',0,3000).gap); assert.equal(o.snapshot('s',0,3000).events.length,1024);
  const generation = o.snapshot('s').generation; created(o,'new'); assert(o.snapshot('s').generation > generation);
});
test('missing candidate source and changed message envelope fail safely', () => {
  assert.throws(() => protocolEvents(num(1,1)));
  const missingSource = Buffer.concat([num(1,1),bytes(3,Buffer.concat([bytes(1,num(1,123)),bytes(2,num(1,456)),num(4,99)]))]);
  assert.throws(() => protocolEvents(missingSource));
});
test('outgoing browser messages allow omitted account source, without treating send as server health', () => {
  const outgoing = Buffer.concat([num(1,1),bytes(3,Buffer.concat([bytes(1,num(1,456)),bytes(2,usr(123,1)),num(4,99)]))]);
  const o = observer(); send(o, pub(outgoing));
  const result = o.snapshot('s',0,3000);
  assert.equal(result.connected,false);
  assert.deepEqual(result.events[0].from,{uid:'456',source:null});
  assert.deepEqual(result.events[0].to,{uid:'123',source:'1'});
  receive(o,Buffer.from([0xd0,0])); assert(o.snapshot('s',0,3000).connected);
});
test('outgoing fragments and control packets do not corrupt the receiving stream', () => {
  const o = observer(), packet = pub();
  send(o,Buffer.from([0xc0,0])); send(o,packet.subarray(0,4));
  receive(o,pub()); send(o,packet.subarray(4));
  const result = o.snapshot('s',0,3000);
  assert(result.connected); assert.equal(result.events.length,2);
});
test('self-message server echo is observed and malformed outgoing publish falls back', () => {
  const outgoing = Buffer.concat([num(1,1),bytes(3,Buffer.concat([bytes(1,num(1,456)),bytes(2,usr(123)),num(4,99)]))]);
  const o = observer(); receive(o,pub(outgoing));
  assert.equal(o.snapshot('s',0,3000).events.length,1);
  send(o,pub(num(1,99))); assert(!o.snapshot('s',0,3000).connected);
});
test('reattaching a CDP session changes stream identity even at the same cursor', () => {
  const o = observer(), epoch = o.snapshot('s').epoch;
  o.remove('s'); o.state('s'); created(o);
  assert.notEqual(o.snapshot('s').epoch, epoch);
});
