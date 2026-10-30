<script setup lang="ts">
// Plain-text instructions for the two keys Kaisen needs (reusable in the User Guide).
defineProps<{ kind: 'anthropic' | 'github'; open?: boolean }>()
</script>

<template>
  <details v-if="kind === 'anthropic'" class="help">
    <summary>How to get an Anthropic API key</summary>
    <ol class="mt-1 list-decimal space-y-1 pl-5 text-sm text-zinc-300">
      <li>Open <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener noreferrer" class="link">console.anthropic.com &gt; API keys<span class="sr-only"> (opens in new tab)</span></a>.</li>
      <li>Create a key named <code class="mono">picopaas-kaisen</code>.</li>
      <li>Paste it above. Set a spend limit on the Anthropic side too.</li>
      <li>Save, then press <strong>Test Anthropic</strong>.</li>
    </ol>
  </details>
  <details v-else class="help" :open="open">
    <summary>How to create the access token (about 2 minutes)</summary>
    <ol class="mt-1 list-decimal space-y-1 pl-5 text-sm text-zinc-300">
      <li>Open <a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener noreferrer" class="link">GitHub &gt; Settings &gt; Developer settings &gt; Fine-grained tokens &gt; Generate new token<span class="sr-only"> (opens in new tab)</span></a>.</li>
      <li><strong>Token name:</strong> <code class="mono">kaisen</code>. <strong>Expiration:</strong> 90 days (you can paste a new one here any time).</li>
      <li><strong>Resource owner:</strong> the account or organization that owns the repository above.</li>
      <li><strong>Repository access:</strong> <em>Only select repositories</em>, then pick just that one repository.</li>
      <li><strong>Repository permissions:</strong> set <em>Contents</em> to <strong>Read and write</strong> and <em>Pull requests</em> to <strong>Read and write</strong>. <em>Metadata: Read-only</em> is added automatically. Leave everything else at <em>No access</em>.</li>
      <li>Press <strong>Generate token</strong>, copy it (it starts with <code class="mono">github_pat_</code>) and paste it above. GitHub shows it only once.</li>
      <li>If the repository belongs to an organization, an owner may need to approve the token under the organization's <em>Personal access tokens</em> settings.</li>
      <li>Save, then press <strong>Test GitHub</strong> below.</li>
    </ol>
    <p class="mt-2 text-sm text-zinc-300">Why only these permissions: Kaisen only needs to create commits and branches and open a pull request. Without Workflows or Administration permission it cannot change your Actions, settings or other repositories. The token is stored encrypted and never shown again.</p>
  </details>
</template>
