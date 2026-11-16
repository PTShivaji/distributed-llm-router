import js from '@eslint/js'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist', 'coverage', 'node_modules', 'src/api/schema.gen.ts'] },
  {
    files: ['**/*.{ts,tsx}'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended, reactHooks.configs.flat['recommended-latest']],
    languageOptions: { ecmaVersion: 2022, globals: globals.browser },
    plugins: { 'react-refresh': reactRefresh },
    rules: {
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      // Secrets must never be read from build-time env vars (they would be embedded in the bundle).
      'no-restricted-syntax': [
        'error',
        {
          selector: "MemberExpression[object.object.meta.name='import'][object.property.name='env'][property.name!=/^(VITE_API_BASE_URL|DEV|PROD|MODE|BASE_URL|SSR)$/]",
          message: 'Only VITE_API_BASE_URL may be read from import.meta.env. Never put secrets in VITE_* variables.',
        },
      ],
    },
  },
  {
    files: ['scripts/**/*.mjs', 'vite.config.ts'],
    languageOptions: { globals: globals.node },
  },
)
