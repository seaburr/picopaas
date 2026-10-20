<script setup lang="ts">
import { ref, useId } from 'vue'
import { Eye, EyeOff } from 'lucide-vue-next'

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
        class="input pr-9"
        @input="$emit('update:modelValue', ($event.target as HTMLInputElement).value)"
      />
      <button
        type="button"
        class="btn-icon btn-icon-sm absolute right-0.5 top-0.5"
        :aria-pressed="shown"
        :aria-label="shown ? `Hide ${label.toLowerCase()}` : `Show ${label.toLowerCase()}`"
        :title="shown ? `Hide ${label.toLowerCase()}` : `Show ${label.toLowerCase()}`"
        @click="shown = !shown"
      >
        <EyeOff v-if="shown" :size="16" aria-hidden="true" />
        <Eye v-else :size="16" aria-hidden="true" />
      </button>
    </div>
    <p v-if="help" :id="`${id}-help`" class="help">{{ help }}</p>
  </div>
</template>
