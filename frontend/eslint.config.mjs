import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";
import pluginVue from "eslint-plugin-vue";
import vueA11y from "eslint-plugin-vuejs-accessibility";

export default tseslint.config(
  { ignores: ["node_modules/**", "dist/**", "test-results/**", "playwright-report/**", "../static/**"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs["flat/recommended"],
  ...vueA11y.configs["flat/recommended"],
  {
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
  },
  {
    files: ["**/*.vue"],
    languageOptions: { parserOptions: { parser: tseslint.parser } },
  },
  {
    rules: {
      // Formatting rules from vue/flat/recommended: the task is bugs and a11y, not style (no Prettier).
      "vue/max-attributes-per-line": "off",
      "vue/singleline-html-element-content-newline": "off",
      "vue/multiline-html-element-content-newline": "off",
      "vue/html-indent": "off",
      "vue/html-self-closing": "off",
      "vue/first-attribute-linebreak": "off",
      "vue/html-closing-bracket-newline": "off",
      // Existing single-word component names (Sparkline etc.) are fine; renaming churns imports for no bug.
      "vue/multi-word-component-names": "off",
      // Optional TS-typed props are undefined by default, which is intended; forcing defaults is noise.
      "vue/require-default-prop": "off",
      // Default demands BOTH nesting and for/id; either one associates the label, which is what matters.
      "vuejs-accessibility/label-has-for": ["error", { required: { some: ["nesting", "id"] } }],
    },
  },
);
