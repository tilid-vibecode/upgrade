import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

const configDir = path.dirname(fileURLToPath(import.meta.url))
const certDir = path.resolve(configDir, '../certs')
const frontendKeyPath = path.join(certDir, 'frontend-key.pem')
const frontendCertPath = path.join(certDir, 'frontend-cert.pem')

function resolveHttpsConfig() {
  if (!fs.existsSync(frontendKeyPath) || !fs.existsSync(frontendCertPath)) {
    return undefined
  }

  return {
    key: fs.readFileSync(frontendKeyPath),
    cert: fs.readFileSync(frontendCertPath),
  }
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')
  const devApiTarget = env.VITE_DEV_API_TARGET || 'https://localhost:8000'
  const https = resolveHttpsConfig()

  return {
    plugins: [react()],
    server: {
      https,
      port: 3000,
      strictPort: true,
      proxy: {
        '/api': {
          target: devApiTarget,
          changeOrigin: true,
          secure: false,
        },
      },
    },
  }
})
