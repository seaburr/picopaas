<script setup lang="ts">
// A sidebar link. `cur` highlights it; aria-current="page" is set only when it is the exact page (`exact`, default).
// Built on RouterLink's custom slot so several links to the same route (every project goes to /apps) do not all
// claim aria-current.
withDefaults(defineProps<{ to: string; cur?: boolean; exact?: boolean }>(), { cur: false, exact: true })
const emit = defineEmits<{ go: [] }>()
</script>

<template>
  <RouterLink v-slot="{ href, navigate }" :to="to" custom>
    <a :href="href" :class="{ cur }" :aria-current="cur && exact ? 'page' : undefined" @click="emit('go'); navigate($event)"><slot /></a>
  </RouterLink>
</template>
