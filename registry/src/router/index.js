import { createRouter, createWebHistory } from 'vue-router';
import store, { isExpired } from '../store/index.js';
import IndexView from '../views/Index.vue';
import Preview from '../views/Preview.vue';
import EditView from '../views/Edit.vue';
import AuthView from '../views/Auth.vue';

const routes = [
  {
    path: '/',
    name: 'index',
    component: IndexView,
  },
  {
    path: '/domains/new',
    name: 'add',
    component: EditView,
  },
  {
    path: '/domains/:id',
    name: 'preview',
    component: Preview,
  },
  {
    path: '/domains/edit/:id',
    name: 'edit',
    component: EditView,
  },
  {
    path: '/auth',
    name: 'auth',
    component: AuthView,
  },
];

const router = createRouter({
  history: createWebHistory(process.env.BASE_URL),
  routes,
  linkExactActiveClass: 'active',
  linkActiveClass: '',
});

router.beforeEach(async (to, from, next) => {
  const isAuth = store.getters.isAuth && !isExpired(store.getters.accessToken);
  if (to.path !== '/auth' && !isAuth) {
    if (
      store.getters.refreshToken &&
      store.getters.expiresAt * 1000 > Date.now()
    ) {
      try {
        await store.dispatch('refresh');
        next();
      } catch (err) {
        next({ path: '/auth', query: { from: to.fullPath } });
      }
    } else {
      next({ path: '/auth', query: { from: to.fullPath } });
    }
  } else if (to.path === '/auth' && isAuth) {
    next('/');
  } else {
    next();
  }
});

export default router;
