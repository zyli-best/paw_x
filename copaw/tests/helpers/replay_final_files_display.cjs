const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');

const root = process.argv[2];
const req = createRequire(path.join(root, 'console/package.json'));
const ts = req('typescript');
const base = path.join(root, 'console/node_modules/@agentscope-ai/chat/components/AgentScopeRuntimeWebUI/core/AgentScopeRuntime');
function load(file, localRequire) {
  const module = { exports: {} };
  const code = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  vm.runInNewContext(code, { module, exports: module.exports, require: localRequire, console });
  return module.exports;
}
const types = load(path.join(base, 'types.tsx'), req);
const Builder = load(path.join(base, 'Response/Builder.tsx'), name => {
  if (name === '../types') return types;
  if (name === '@agentscope-ai/chat') return { uuid: () => 'unused-error-id' };
  return req(name);
}).default;

const scenario = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const builder = new Builder(scenario.events[0]);
function submissions(messages) {
  return messages.flatMap(message => message.content ?? [])
    .filter(content => content.data?.name === 'send_final_files_to_user');
}
for (const [index, event] of scenario.events.entries()) {
  builder.handle(event);
  const selected = submissions(builder.data.output);
  if (index < scenario.before_verdict) assert.equal(selected.length, 0);
  if (scenario.outcome !== 'pass') {
    assert.ok(!JSON.stringify(selected).includes('candidate.txt'));
  }
}
assert.deepEqual(JSON.parse(JSON.stringify(builder.data.output)), scenario.events.at(-1).output);
const selected = Builder.mergeToolMessages(builder.data.output)
  .filter(message => submissions([message]).length);
assert.equal(selected.length, 1);
if (selected.length) {
  assert.equal(selected[0].content[0].data.name, selected[0].content[1].data.name);
  assert.equal(selected[0].content[1].data.state, 'success');
}
console.log('Final-file tool frontend replay passed');
