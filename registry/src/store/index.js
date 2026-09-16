import { createStore } from 'vuex';
import router from '../router';
import Auth from '../services/auth.service';

export function isExpired(token) {
  if (!token) return true;
  try {
    const payload = JSON.parse(
      atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))
    );
    return !Number.isFinite(payload.exp) || payload.exp * 1000 <= Date.now();
  } catch (err) {
    return true;
  }
}

export default createStore({
  state: {
    accessToken: localStorage.getItem('__accsToken') || null,
    refreshToken: localStorage.getItem('__rfshToken') || null,
    isAuth: !isExpired(localStorage.getItem('__accsToken')) || false,
    user: JSON.parse(localStorage.getItem('__usr') || 'null'),
    expiresAt: localStorage.getItem('__exp') || null,
    loading: false,
  },
  getters: {
    isAuth: (state) => state.isAuth,
    accessToken: (state) => state.accessToken,
    refreshToken: (state) => state.refreshToken,
    expiresAt: (state) => state.expiresAt,
    user: (state) => state.user,
    loading: (state) => state.loading,
  },
  mutations: {
    setTokens(
      state,
      { access_token: accs, refresh_token: rfsh, expires_at: expiresAt }
    ) {
      state.accessToken = accs;
      state.refreshToken = rfsh;
      state.expiresAt = expiresAt;
      state.isAuth = !isExpired(accs);
      localStorage.setItem('__accsToken', accs);
      if (rfsh) localStorage.setItem('__rfshToken', rfsh);
      if (expiresAt) localStorage.setItem('__exp', expiresAt);
    },
    clearSession(state) {
      state.accessToken = null;
      state.refreshToken = null;
      state.expiresAt = null;
      state.isAuth = false;
      localStorage.removeItem('__accsToken');
      localStorage.removeItem('__rfshToken');
      localStorage.removeItem('__exp');
      state.user = null;
      localStorage.removeItem('__usr');
    },
    setUser(state, user) {
      state.user = user;
      localStorage.setItem('__usr', JSON.stringify(user));
    },
    setLoading(state, value) {
      state.loading = value;
    },
  },
  actions: {
    async login({ commit }, creds) {
      return Auth.login(creds).then(({ body, status }) => {
        commit('setTokens', body);
        commit('setUser', { email: body.email, uid: body.uid });
        return status;
      });
    },
    async refresh({ state, commit }) {
      return Auth.refresh(state.refreshToken)
        .then(({ body }) => {
          commit('setTokens', body);
          commit('setUser', { email: body.email, uid: body.uid });
          return body.access_token;
        })
        .catch((err) => {
          commit('clearSession');
          throw err;
        });
    },
    async logout({ commit }) {
      commit('clearSession');
      router.push('/');
    },
  },
  modules: {},
});
