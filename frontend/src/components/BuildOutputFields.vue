<script setup lang="ts">
import KaisenKeyHelp from './KaisenKeyHelp.vue'
import PasswordField from './PasswordField.vue'
import type { KaisenOutputForm, KaisenSecretState } from '../types'

// Output fields shared by the build page (edit in place) and New build. Secrets are write-only.
defineProps<{
  idPrefix: string
  tokenState?: KaisenSecretState | null // saved per-build token, when there is one
  projectTokenSet: boolean
  disabled?: boolean
  errors?: Record<string, string>
}>()
const form = defineModel<KaisenOutputForm>({ required: true })
</script>

<template>
  <div class="space-y-3">
    <fieldset :disabled="disabled">
      <legend class="label">Where does the code go?</legend>
      <div class="flex flex-col gap-1">
        <label class="flex items-center gap-2 text-sm text-zinc-200"><input v-model="form.output_mode" type="radio" :name="`${idPrefix}-mode`" value="download" class="check" /> Download only</label>
        <label class="flex items-center gap-2 text-sm text-zinc-200"><input v-model="form.output_mode" type="radio" :name="`${idPrefix}-mode`" value="github" class="check" :aria-controls="`${idPrefix}-github`" /> Download and push to GitHub</label>
      </div>
    </fieldset>

    <div v-if="form.output_mode === 'github'" :id="`${idPrefix}-github`" class="space-y-3">
      <div class="grid gap-3 sm:grid-cols-2">
        <div>
          <label :for="`${idPrefix}-repo`" class="label">Repository URL</label>
          <input :id="`${idPrefix}-repo`" v-model="form.github_repo" class="input" placeholder="https://github.com/you/repo" autocomplete="off" :disabled="disabled" :aria-invalid="errors?.github_repo ? 'true' : undefined" />
          <p v-if="errors?.github_repo" class="field-error" role="alert">{{ errors.github_repo }}</p>
        </div>
        <div>
          <label :for="`${idPrefix}-branch`" class="label">Branch</label>
          <input :id="`${idPrefix}-branch`" v-model="form.github_branch" class="input" placeholder="kaisen/<slug>" autocomplete="off" :disabled="disabled" />
          <p class="help">Leave blank to use kaisen/&lt;slug&gt;.</p>
        </div>
      </div>

      <fieldset :disabled="disabled">
        <legend class="label">GitHub access</legend>
        <div class="flex flex-col gap-1">
          <label class="flex items-center gap-2 text-sm" :class="projectTokenSet ? 'text-zinc-200' : 'text-zinc-500'">
            <input v-model="form.github_auth" type="radio" :name="`${idPrefix}-auth`" value="project" class="check" :disabled="!projectTokenSet" /> Use the project's GitHub token
          </label>
          <p v-if="!projectTokenSet" class="help ml-6">The project has no GitHub token. <RouterLink to="/kaisen/settings" class="link">Add one in Kaisen settings</RouterLink></p>
          <label class="flex items-center gap-2 text-sm text-zinc-200">
            <input v-model="form.github_auth" type="radio" :name="`${idPrefix}-auth`" value="build" class="check" /> Use a token for this build only
          </label>
        </div>
      </fieldset>
      <template v-if="form.github_auth === 'build'">
        <PasswordField v-model="form.github_token" label="Access token" autocomplete="off" :required="false" :invalid="!!errors?.github_token" :help="tokenState?.set ? `Saved, ends in ...${tokenState.last4 ?? ''}. Leave blank to keep the saved token.` : 'Not set'" />
        <p v-if="errors?.github_token" class="field-error" role="alert">{{ errors.github_token }}</p>
        <label v-if="tokenState?.set" class="flex items-center gap-2 text-sm text-zinc-200"><input v-model="form.clear_github_token" type="checkbox" class="check" :disabled="disabled" /> Clear stored token</label>
        <KaisenKeyHelp kind="github" :open="!tokenState?.set" />
      </template>

      <div class="flex flex-col gap-1">
        <label class="flex items-center gap-2 text-sm text-zinc-200"><input v-model="form.push_on_finish" type="checkbox" class="check" :disabled="disabled" /> Push automatically when a build finishes</label>
        <label class="flex items-center gap-2 text-sm text-zinc-200"><input v-model="form.open_pr" type="checkbox" class="check" :disabled="disabled" /> Open a pull request after pushing</label>
      </div>
    </div>
  </div>
</template>
