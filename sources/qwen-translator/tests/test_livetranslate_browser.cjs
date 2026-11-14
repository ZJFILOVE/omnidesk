const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const app = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const handler = app.slice(app.indexOf('function handleServerMessage(event) {'),
                          app.indexOf('\nasync function start()'));

function client() {
  const context = vm.createContext({
    captureRequestId() {}, setStatus() {}, renderSource() {}, renderTranslation() {},
  });
  vm.runInContext('let sourceDraft = "", translationDraft = "", lastTranslationPartial = "";\n' + handler, context);
  return {
    send(event) {
      context.message = {data: JSON.stringify({type: 'model_event', ...event})};
      vm.runInContext('handleServerMessage(message)', context);
    },
    drafts() {
      return JSON.parse(vm.runInContext('JSON.stringify([sourceDraft, translationDraft])', context));
    },
  };
}

test('3.8 source and translation deltas preserve repeated chunks and whitespace', () => {
  const c = client();
  for (const chunk of ['Very', ' ', 'very', ' ', 'very', ' good.']) {
    c.send({source_delta: chunk});
    c.send({translation_delta: chunk});
  }
  assert.deepEqual(c.drafts(), ['Very very very good.', 'Very very very good.']);
});

test('a new response starts a fresh translation without discarding source transcription', () => {
  const c = client();
  c.send({source_delta: '原文'});
  c.send({translation_delta: 'Previous response'});
  c.send({response_started: true});
  c.send({translation_delta: 'New response'});
  assert.deepEqual(c.drafts(), ['原文', 'New response']);
});
