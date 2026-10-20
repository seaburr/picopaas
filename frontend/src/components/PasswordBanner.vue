<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const show = computed(() => auth.user?.must_change_password && route.path !== '/account')
</script>

<template>
  <div v-if="show" role="alert" class="alert-warn rounded-none border-x-0 border-t-0 px-4 py-2 text-center">
    An administrator reset your password. <RouterLink to="/account" class="font-semibold underline underline-offset-2">Change it now</RouterLink>.
  </div>
</template>
