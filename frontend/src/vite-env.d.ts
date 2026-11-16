/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** The ONLY frontend environment variable. Never add secrets to VITE_* variables. */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
