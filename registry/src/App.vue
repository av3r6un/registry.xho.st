<template>
  <Header v-if="notOnAuth" />
  <router-view class="content" />
</template>
<script>
import Header from './components/Header.vue';

export default {
  name: 'App',
  components: { Header },
  beforeMount() {
    const lang = navigator.language;
    if (this.$i18n.availableLocales.includes(lang)) this.$i18n.locale = lang;
  },
  computed: {
    notOnAuth() {
      return this.$route.path !== '/auth';
    },
  },
};
</script>
<style lang="scss">
#app {
  min-height: 100vh;
  width: 100%;
  max-width: 1440px;
  margin: 0 auto;
  padding-top: 70px;
}
</style>
