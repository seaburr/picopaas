<script setup lang="ts">
import { ref, useId } from 'vue'

// Labelled password input with a show/hide toggle and an optional persistent helper line.
withDefaults(
  defineProps<{
    modelValue: string
    label?: string
    autocomplete?: string
    minlength?: number
    help?: string
    invalid?: boolean
    required?: boolean
  }>(),
  { label: 'Password', autocomplete: 'current-password', required: true },
)
defineEmits<{ 'update:modelValue': [v: string] }>()
const id = useId()
const shown = ref(false)
</script>

<template>
  <div>
    <label :for="id" class="label">{{ label }}</label>
    <div class="relative">
      <input
        :id="id"
        :value="modelValue"
        :type="shown ? 'text' : 'password'"
        :required="required"
        :minlength="minlength"
        :autocomplete="autocomplete"
        :aria-invalid="invalid ? 'true' : undefined"
        :aria-describedby="help ? `${id}-help` : undefined"
        class="input pr-16"
        @input="$emit('update:modelValue', ($event.target as HTMLInputElement).value)"
      />
      <button
        type="button"
        class="absolute inset-y-0 right-0 rounded-r-md px-3 text-xs font-medium text-zinc-300 hover:text-white"
        :aria-pressed="shown"
        :aria-label="shown ? `Hide ${label.toLowerCase()}` : `Show ${label.toLowerCase()}`"
        @click="shown = !shown"
      >
        {{ shown ? 'Hide' : 'Show' }}
      </button>
    </div>
    <p v-if="help" :id="`${id}-help`" class="help">{{ help }}</p>
  </div>
</template>
