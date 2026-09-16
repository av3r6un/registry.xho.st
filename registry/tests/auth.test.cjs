const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const { createStore } = require('vuex');

function source(file) {
  return fs
    .readFileSync(path.join(__dirname, '../src', file), 'utf8')
    .replace(/^import .*;\s*$/gm, '');
}

function token(exp) {
  return `header.${Buffer.from(JSON.stringify({ exp })).toString(
    'base64url'
  )}.signature`;
}

function session(Auth) {
  const storage = new Map();
  const localStorage = {
    getItem: (key) => storage.get(key) || null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
  };
  const script = source('store/index.js')
    .replace('export function', 'function')
    .replace('export default', 'const store =');
  return {
    ...new Function(
      'createStore',
      'router',
      'Auth',
      'localStorage',
      `${script}\nreturn { store, isExpired };`
    )(createStore, {}, Auth, localStorage),
    storage,
  };
}

const body = () => ({
  access_token: token(Math.floor(Date.now() / 1000) + 3600),
  refresh_token: 'refresh-rotated',
  expires_at: Math.floor(Date.now() / 1000) + 86400,
  uid: 'ABC123',
  email: 'user@example.com',
});

test('login and refresh preserve rotated tokens and expiry', async () => {
  const first = body();
  const next = { ...body(), refresh_token: 'refresh-next' };
  const { store, storage } = session({
    login: async () => ({ body: first, status: 'ok' }),
    refresh: async (refresh) => {
      assert.equal(refresh, first.refresh_token);
      return { body: next };
    },
  });
  await store.dispatch('login', {});
  assert.equal(store.state.refreshToken, first.refresh_token);
  assert.equal(store.state.expiresAt, first.expires_at);
  assert.equal(await store.dispatch('refresh'), next.access_token);
  assert.equal(storage.get('__rfshToken'), next.refresh_token);
});

test('failed login rejects and failed refresh clears the session', async () => {
  const { store, storage } = session({
    login: async () => {
      throw new Error('unauthorized');
    },
    refresh: async () => {
      throw new Error('revoked');
    },
  });
  await assert.rejects(store.dispatch('login', {}), /unauthorized/);
  store.commit('setTokens', body());
  store.commit('setUser', { uid: 'ABC123' });
  await assert.rejects(store.dispatch('refresh'), /revoked/);
  assert.equal(store.state.isAuth, false);
  assert.equal(store.state.user, null);
  assert.equal(storage.size, 0);
});

test('auth refresh sends the JSON contract and unwraps Axios data', async () => {
  let request;
  const response = { body: body() };
  const Auth = new Function(
    'axios',
    source('services/auth.service.js').replace(
      'export default Auth;',
      'return Auth;'
    )
  )({
    create: () => ({
      post: async (...args) => {
        request = args;
        return { data: response };
      },
    }),
  });
  assert.equal(await Auth.refresh('refresh-input'), response);
  assert.deepEqual(request, [
    '/refresh',
    { data: { refresh_token: 'refresh-input' } },
  ]);
});

test('parallel API requests refresh once and use the new Bearer token', async () => {
  let refreshes = 0;
  const { store, isExpired } = session({
    refresh: async () => {
      refreshes += 1;
      await Promise.resolve();
      return { body: body() };
    },
  });
  store.commit('setTokens', { ...body(), access_token: token(1) });
  let interceptor;
  new Function(
    'axios',
    'store',
    'isExpired',
    source('services/axios.service.js').replace(
      'export default api;',
      'return api;'
    )
  )(
    {
      create: () => ({
        interceptors: {
          request: {
            use: (callback) => {
              interceptor = callback;
            },
          },
          response: { use: () => {} },
        },
      }),
    },
    store,
    isExpired
  );
  const requests = await Promise.all(
    [0, 1].map(() => interceptor({ method: 'get', headers: new Map() }))
  );
  assert.equal(refreshes, 1);
  for (const request of requests) {
    assert.equal(
      request.headers.Authorization,
      `Bearer ${store.state.accessToken}`
    );
  }
});

test('router refreshes an expired token even with a cached isAuth flag', async () => {
  const { store, isExpired } = session({
    refresh: async () => ({ body: body() }),
  });
  store.commit('setTokens', { ...body(), access_token: token(1) });
  store.state.isAuth = true;
  let guard;
  new Function(
    'createRouter',
    'createWebHistory',
    'store',
    'isExpired',
    'IndexView',
    'Preview',
    'EditView',
    'AuthView',
    source('router/index.js').replace(
      'export default router;',
      'return router;'
    )
  )(
    () => ({
      beforeEach: (callback) => {
        guard = callback;
      },
    }),
    () => ({}),
    store,
    isExpired,
    {},
    {},
    {},
    {}
  );
  const calls = [];
  await guard({ path: '/domains/1', fullPath: '/domains/1' }, {}, (...args) =>
    calls.push(args)
  );
  assert.deepEqual(calls, [[]]);
  assert.equal(isExpired(store.state.accessToken), false);
});
