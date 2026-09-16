import axios from 'axios';
import store, { isExpired } from '../store';

const api = axios.create({
  baseURL: '/api',
  validateStatus: (status) => status >= 200 && status < 300,
});

let refreshing = null;

api.interceptors.request.use(
  async (config) => {
    if (
      isExpired(store.getters.accessToken) &&
      store.getters.refreshToken &&
      store.getters.expiresAt * 1000 > Date.now()
    ) {
      refreshing ||= store.dispatch('refresh').finally(() => {
        refreshing = null;
      });
      await refreshing;
    }
    const { headers } = config;
    headers.Authorization = `Bearer ${store.getters.accessToken}`;
    config.headers.set('Accept', 'application/json');

    if (['post', 'put', 'patch'].includes(config.method)) {
      config.headers.set('Content-Type', 'application/json');
    }

    return config;
  },
  (err) => Promise.reject(err)
);

api.interceptors.response.use(
  (res) => res,
  (err) => Promise.reject(err)
);

export default api;
