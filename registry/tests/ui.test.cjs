const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');

function component(file) {
  const source = fs.readFileSync(path.join(__dirname, '../src', file), 'utf8');
  const script = source
    .match(/<script>([\s\S]*?)<\/script>/)[1]
    .replace(/^import .*;\s*$/gm, '')
    .replace('export default', 'return');
  return new Function(
    'Backend',
    'Input',
    'Selection',
    'mIcon',
    'Domains',
    script
  )(class {}, {}, {}, {}, {});
}

test('preview selects the API latest deployment and offers apply for an active rule draft', () => {
  const view = component('views/Preview.vue');
  const domain = {
    enabled: true,
    latest_deployment: { id: 2, config_text: 'NEW' },
    applied_deployment: { id: 1 },
  };
  const context = { domain };
  context.latestDeployment = view.computed.latestDeployment.call(context);
  assert.equal(context.latestDeployment.config_text, 'NEW');
  assert.ok(view.computed.canApply.call(context));
});

test('edit sends current name and splits aliases with commas or spaces', () => {
  const view = component('views/Edit.vue');
  const context = {
    record: {
      name: 'example.com',
      type: 'hostname',
      server_names: [],
      route: {},
    },
  };
  view.computed.serverNames.set.call(
    context,
    'www.example.com,api.example.com test.example.com'
  );
  const payload = view.methods.buildPayload.call(context);
  assert.deepEqual(payload.server_names, [
    'example.com',
    'www.example.com',
    'api.example.com',
    'test.example.com',
  ]);
});

test('API errors use the backend message field', async () => {
  let source = fs.readFileSync(
    path.join(__dirname, '../src/services/backend.service.js'),
    'utf8'
  );
  source = source
    .replace(/^import .*;\s*$/gm, '')
    .replace('export default Backend;', 'return Backend;');
  const Backend = new Function(source)();
  const backend = new Backend();
  const error = {
    response: {
      status: 400,
      statusText: 'Bad Request',
      data: { message: 'invalid_upstream_port' },
    },
  };
  await assert.rejects(backend.manageError(error));
  assert.match(backend.msg, /invalid_upstream_port/);
});
