// Scans the production bundle (dist/) for anything that looks like a secret.
//   npm run build && npm run check:bundle
// Fails (exit 1) on: provider-key / token-shaped strings, the dev DB password, VITE_* names
// other than VITE_API_BASE_URL, source maps, or the VALUE of any secret-like environment
// variable present in this shell (values are compared, never printed).
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const dist = fileURLToPath(new URL('../dist', import.meta.url))

function files(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    return statSync(path).isDirectory() ? files(path) : [path]
  })
}

const PATTERNS = [
  ['OpenAI/Anthropic-style key', /\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}/],
  ['Google API key', /\bAIza[0-9A-Za-z_-]{30,}/],
  ['Groq key', /\bgsk_[A-Za-z0-9]{20,}/],
  ['hard-coded bearer token', /Bearer\s+[A-Za-z0-9._~+/-]{16,}/],
  ['development database password', /router_password/],
  ['unexpected VITE_* variable', /\bVITE_(?!API_BASE_URL\b)[A-Z0-9_]+/],
]

const secretEnv = Object.entries(process.env)
  .filter(([name, value]) => /(KEY|TOKEN|SECRET|PASSWORD)/i.test(name) && typeof value === 'string' && value.length >= 12)

let all
try {
  all = files(dist)
} catch {
  console.error('dist/ not found: run `npm run build` first')
  process.exit(1)
}

const problems = []
for (const file of all) {
  const rel = relative(dist, file)
  if (rel.endsWith('.map')) problems.push(`${rel}: source map in production bundle`)
  if (!/\.(js|css|html|svg|json|txt)$/.test(rel)) continue
  const text = readFileSync(file, 'utf8')
  for (const [label, re] of PATTERNS) if (re.test(text)) problems.push(`${rel}: ${label}`)
  for (const [name, value] of secretEnv) if (text.includes(value)) problems.push(`${rel}: contains the value of env var ${name}`)
}

if (problems.length) {
  console.error(`Bundle check FAILED (${problems.length}):\n  - ${problems.join('\n  - ')}`)
  process.exit(1)
}
console.log(`Bundle check passed: ${all.length} files scanned, no secrets or extra VITE_* variables, no source maps ` +
  `(${secretEnv.length} secret-like env values compared).`)
