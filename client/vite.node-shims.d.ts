declare module 'node:fs' {
  const fs: {
    existsSync: (...args: unknown[]) => boolean
    readFileSync: (...args: unknown[]) => string | Uint8Array
  }

  export default fs
}

declare module 'node:path' {
  const path: {
    dirname: (value: string) => string
    resolve: (...parts: string[]) => string
    join: (...parts: string[]) => string
  }

  export default path
}

declare module 'node:url' {
  export function fileURLToPath(value: string): string
}

interface ImportMeta {
  url: string
}
