<template>
  <article class="auth">
    <div class="auth_wrapper">
      <div class="auth_title centered">{{ $t('auth.title') }}</div>
      <form class="auth_form" @submit.prevent="login">
        <Input
          type="email"
          autofocus
          required
          v-model="authForm.email"
          :placeholder="$t('placeholders.email')"
          name="auth.email"
        />
        <Input
          type="password"
          required
          v-model="authForm.password"
          :placeholder="$t('placeholders.password')"
          name="auth.password"
        />
        <button type="submit" class="btn btn_submit wide">
          {{ $t('auth.submit') }}
        </button>
        <p v-if="loginError" role="alert">{{ loginError }}</p>
      </form>
    </div>
  </article>
</template>
<script>
import Input from '../components/Input.vue';

export default {
  name: 'AuthView',
  components: { Input },
  data() {
    return {
      loginError: null,
      authForm: {
        email: null,
        password: null,
        token_use: 'access',
      },
    };
  },
  methods: {
    login() {
      this.loginError = null;
      this.$store
        .dispatch('login', { data: this.authForm })
        .then(() => this.$router.push(this.$route.query.from || '/'))
        .catch((err) => {
          this.loginError = err.response?.data?.message || err.message;
        });
    },
  },
};
</script>
<style lang="scss" scoped>
.auth {
  &_wrapper {
    max-width: 400px;
    margin: 0 auto;
  }
  &_title {
    font-size: 24px;
    font-weight: 600;
  }
  &_form {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-top: 12px;
  }
}
</style>
