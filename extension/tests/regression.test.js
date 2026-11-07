const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = (name) => fs.readFileSync(path.join(__dirname, '..', name), 'utf8');

test('content and popup default to disabled, including storage failure', () => {
  function content(fail) {
    const attrs = {}, messages = [];
    const context = {
      location: {hostname: 'one.google.com'},
      document: {documentElement: {setAttribute: (key, value) => { attrs[key] = value; }}},
      window: {postMessage: (message) => messages.push(message), addEventListener() {}},
      chrome: {storage: {local: {get(keys, cb) { if (fail && keys[0] === 'gapt_layer') throw Error(); cb({}); }}, onChanged: {addListener() {}}}, runtime: {onMessage: {addListener() {}}}},
      setTimeout() {},
    };
    vm.runInNewContext(source('content.js'), context);
    assert.equal(attrs['data-gapt-layer'], 'off');
    assert.equal(messages[0].layer, 'off');
  }
  content(false);
  content(true);
  const inputs = ['off', '12m'].map(value => ({value, addEventListener() {}}));
  const element = {setAttribute() {}};
  vm.runInNewContext(source('popup.js'), {
    document: {getElementById: () => element, querySelectorAll: () => inputs},
    chrome: {storage: {local: {get: (keys, cb) => cb({})}}, tabs: {query: (opts, cb) => cb([])}, runtime: {}},
    setTimeout() {}, setInterval() {},
  });
  assert.equal(inputs[0].checked, true);
  assert.equal(inputs[1].checked, false);
});

test('XHR form decoding preserves escaped characters and rewrites multiple RPC calls', () => {
  const listeners = {};
  function XHR() {}
  XHR.prototype.send = function (body) { this.body = body; };
  function Element() {}
  Element.prototype.setAttribute = function () {};
  function Frame() {}
  const context = {
    XMLHttpRequest: XHR, Element, HTMLIFrameElement: Frame, URL,
    location: {origin: 'https://one.google.com'},
    document: {body: null, documentElement: {getAttribute: () => '12m'}, addEventListener() {}},
    window: {addEventListener: (type, cb) => { listeners[type] = cb; }, postMessage() {}},
  };
  vm.runInNewContext(source('inject.js'), context);
  const payload = () => { const node = Array(13).fill(null); node[12] = []; return [node]; };
  const extra = 'literal+plus \\ slash & ampersand';
  const calls = [[['Ngbm', JSON.stringify(payload()), extra], ['Ngbm', JSON.stringify(payload()), extra]]];
  const encoded = encodeURIComponent(JSON.stringify(calls)).replace(/%20/g, '+');
  const xhr = new XHR();
  xhr.send('before=a%26b&f.req=' + encoded + '&after=x%2By');
  assert.match(xhr.body, /^before=a%26b&f\.req=.*&after=x%2By$/);
  const decoded = JSON.parse(new URLSearchParams(xhr.body).get('f.req'));
  for (const call of decoded[0]) {
    assert.equal(call[2], extra);
    const rewritten = JSON.parse(call[1]);
    assert.match(rewritten[0][12][0][0][0], /g1\.2tb\.ai\.subs_variant$/);
  }
});
