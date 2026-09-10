// Exercise the actual page scripts with controlled peers, fetches, and timers.
// These tests validate lifecycle behavior; they do not emulate WebRTC media.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const script = page => fs.readFileSync(path.join(__dirname, '..', 'app', page), 'utf8')
  .match(/<script>([\s\S]*?)<\/script>/)[1];
const flush = () => new Promise(resolve => setImmediate(resolve));
const response = (status = 200, body = {type: 'answer', sdp: 'fixture'}) => ({
  ok: status >= 200 && status < 300, status, json: async () => body
});

function harness(page, fetchImpl = async () => response()) {
  const peers = [], calls = [], timers = new Map(), listeners = new Map();
  const elements = new Map();
  const element = () => ({
    style: {}, dataset: {}, classList: {add() {}, remove() {}},
    getContext: () => ({}), addEventListener() {},
    querySelector() { return this.child ||= element(); }
  });
  class Peer {
    constructor() {
      this.connectionState = 'new';
      this.iceGatheringState = 'complete';
      peers.push(this);
    }
    createDataChannel() { return {}; }
    addTrack() {}
    getTransceivers() { return []; }
    getSenders() { return []; }
    getReceivers() { return []; }
    async createOffer() { return {type: 'offer', sdp: 'fixture'}; }
    async createAnswer() { return {type: 'answer', sdp: 'fixture'}; }
    async setLocalDescription(value) { this.localDescription = value; }
    async setRemoteDescription(value) {
      if (!value.sdp) throw new Error('Invalid fixture SDP');
      this.remoteDescription = value;
    }
    close() { this.connectionState = 'closed'; }
    state(value) { this.connectionState = value; this.onconnectionstatechange?.(); }
  }
  const capture = () => {
    const track = {readyState: 'live', stop() { this.readyState = 'ended'; }};
    return {getVideoTracks: () => [track], getTracks: () => [track]};
  };
  let timerId = 0;
  const context = vm.createContext({
    console: {error() {}}, AbortController, DOMException,
    BroadcastChannel: class {postMessage() {}}, RTCPeerConnection: Peer,
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
      },
      querySelector: () => element(), addEventListener() {}, body: element()
    },
    window: {
      addEventListener(name, callback) {
        if (!listeners.has(name)) listeners.set(name, new Set());
        listeners.get(name).add(callback);
      },
      removeEventListener(name, callback) { listeners.get(name)?.delete(callback); }
    },
    navigator: {},
    setTimeout(callback, delay) { timers.set(++timerId, {callback, delay}); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    setInterval() { return 1; }, clearInterval() {},
    fetch(url, options) { calls.push({url, options}); return fetchImpl(url, options); },
    fixtureCapture: capture(), nextCapture: capture()
  });
  vm.runInContext(script(page), context, {filename: page});
  if (page === 'cast.html') vm.runInContext('stream = fixtureCapture', context);
  return {
    peers, calls, listeners,
    run: code => vm.runInContext(code, context),
    async tick(delay) {
      const due = [...timers.entries()].filter(([, timer]) => timer.delay === delay);
      assert.ok(due.length, `Expected a ${delay}ms timer`);
      for (const [id, timer] of due) { timers.delete(id); timer.callback(); }
      await flush();
    }
  };
}

test('caster reconnect negotiates once despite duplicate failure events', async () => {
  const h = harness('cast.html');
  await h.run('connect()');
  h.peers[0].state('disconnected');
  h.peers[0].state('failed');
  await h.tick(1000);
  assert.equal(h.peers.length, 2);
  assert.ok(h.peers[1].remoteDescription);
  assert.equal(h.peers[0].connectionState, 'closed');
  assert.equal(h.calls.filter(call => call.url === '/offer').length, 2);
});

test('caster retries rejected signaling responses with backoff', async () => {
  let offers = 0;
  const h = harness('cast.html', async url =>
    response(url === '/offer' && ++offers === 1 ? 503 : 200));
  const pending = h.run('reconnect()');
  await h.tick(1000);
  await h.tick(1500);
  await pending;
  assert.equal(offers, 2);
  assert.ok(h.peers[1].remoteDescription);
});

test('stopping during retry cannot replace a later capture', async () => {
  const h = harness('cast.html');
  await h.run('connect()');
  h.run('reconnect()');
  h.run('stopCast(); stream = nextCapture');
  await h.run('connect()');
  const current = h.peers[1];
  await h.tick(1000);
  assert.equal(h.peers.length, 2);
  assert.equal(h.run('pc'), current);
  assert.ok(current.remoteDescription);
});

test('a peer failure while awaiting an answer restarts the retry attempt', async () => {
  let answers = 0;
  const h = harness('cast.html', async url =>
    response(url === '/answer' && ++answers === 1 ? 204 : 200));
  const pending = h.run('reconnect()');
  await h.tick(1000);
  h.peers[0].state('failed');
  await h.tick(500);
  await h.tick(1500);
  await pending;
  assert.equal(h.peers.length, 2);
  assert.ok(h.peers[1].remoteDescription);
});

test('late reset response cannot resurrect a stopped cast', async () => {
  let resolveReset, resets = 0;
  const h = harness('cast.html', url => {
    if (url === '/reset' && ++resets === 1) {
      return new Promise(resolve => { resolveReset = resolve; });
    }
    return Promise.resolve(response());
  });
  const pending = assert.rejects(h.run('connect()'), {name: 'AbortError'});
  h.run('stopCast()');
  resolveReset(response());
  await pending;
  assert.equal(h.peers.length, 0);
  assert.equal(h.run('pc'), null);
});

test('late answer cannot modify a replacement peer', async () => {
  let resolveAnswer, answers = 0;
  const h = harness('cast.html', url => {
    if (url === '/answer' && ++answers === 1) {
      return new Promise(resolve => { resolveAnswer = resolve; });
    }
    return Promise.resolve(response());
  });
  const pending = assert.rejects(h.run('connect()'), {name: 'AbortError'});
  await flush();
  await h.run('connect()');
  resolveAnswer(response());
  await pending;
  assert.equal(h.peers[0].remoteDescription, undefined);
  assert.ok(h.peers[1].remoteDescription);
});

test('viewer retries answer failures and removes old resize listeners', async () => {
  let answers = 0;
  const h = harness('view.html', async url => {
    if (url === '/answer') {
      h.peers.at(-1).ondatachannel({channel: {readyState: 'open', send() {}}});
      return response(++answers === 1 ? 503 : 200);
    }
    return response(200, {type: 'offer', sdp: 'fixture'});
  });
  await flush();
  assert.equal(h.peers[0].connectionState, 'closed');
  assert.equal(h.listeners.get('resize').size, 0);
  await h.tick(1000);
  assert.equal(h.peers.length, 2);
  assert.equal(h.listeners.get('resize').size, 1);
  h.peers[1].state('disconnected');
  h.peers[1].state('failed');
  await flush();
  assert.equal(h.listeners.get('resize').size, 0);
  await h.tick(1000);
  assert.equal(h.peers.length, 3);
});

test('viewer retries invalid descriptions instead of abandoning connect', async () => {
  let offers = 0;
  const h = harness('view.html', async url => response(200,
    url === '/offer' && ++offers === 1 ? {} : {type: 'offer', sdp: 'fixture'}));
  await flush();
  assert.equal(h.peers[0].connectionState, 'closed');
  await h.tick(1000);
  assert.ok(h.peers[1].localDescription);
});

test('viewer retries network errors while waiting for a cast', async () => {
  let calls = 0;
  const h = harness('view.html', async () => {
    if (++calls === 1) throw new Error('fixture network unavailable');
    return response(200, {type: 'offer', sdp: 'fixture'});
  });
  await flush();
  await h.tick(1000);
  assert.ok(h.peers[0].localDescription);
});

test('all inline page scripts parse', () => {
  for (const page of ['cast.html', 'view.html', 'latency-test.html']) new vm.Script(script(page));
});

for (const name of ['NotAllowedError', 'AbortError']) {
  test(`window picker ${name} releases listeners and allows another attempt`, async () => {
    const h = harness('cast.html');
    await h.run(`
      stream = null;
      navigator.mediaDevices = {getDisplayMedia: async () => {
        throw new DOMException('fixture picker failure', '${name}');
      }};
      startCast();
    `);
    assert.equal(h.run("document.getElementById('startBtn').disabled"), false);
    assert.equal(h.listeners.get('focus').size, 0);
    const status = h.run("document.getElementById('status').textContent");
    assert.equal(status, name === 'NotAllowedError' ? 'Cancelled.' : 'Error: fixture picker failure');
  });
}
