/**
 * Static guard: application source must not contain secrets or read secret env vars.
 * (The production bundle is checked separately by `npm run check:bundle`.)
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const SRC = join(process.cwd(), 'src')

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return name === 'test' ? [] : sourceFiles(path)
    return /\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : []
  })
}

describe('frontend source contains no secrets', () => {
  const files = sourceFiles(SRC)

  it('finds the application sources', () => {
    expect(files.length).toBeGreaterThan(10)
  })

  it('reads no environment variable other than VITE_API_BASE_URL', () => {
    for (const file of files) {
      const names = [...readFileSync(file, 'utf8').matchAll(/import\.meta\.env\.([A-Z_]+)/g)].map((m) => m[1])
      expect(names.filter((n) => n !== 'VITE_API_BASE_URL'), file).toEqual([])
    }
  })

  it('contains no key- or token-shaped literals', () => {
    const patterns = [/\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}/, /\bAIza[0-9A-Za-z_-]{30,}/, /\bgsk_[A-Za-z0-9]{20,}/,
      /Bearer\s+[A-Za-z0-9._~+/-]{16,}/, /router_password/]
    for (const file of files) {
      const text = readFileSync(file, 'utf8')
      for (const re of patterns) expect(re.test(text), `${file} matches ${re}`).toBe(false)
    }
  })

  it('never uses localStorage for credentials', () => {
    for (const file of files) expect(/localStorage\s*[.[]/.test(readFileSync(file, 'utf8')), file).toBe(false)
  })
})
