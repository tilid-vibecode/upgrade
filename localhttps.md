# Local HTTPS Setup

## Why HTTPS locally?

Several APIs and browser features require a secure context (`https://`). Without local HTTPS you'll see errors from OAuth providers, Clipboard API, and other services that reject plain HTTP origins. Running both the frontend and backend under HTTPS locally eliminates these issues entirely.

## Quick start (2 minutes)

```bash
# 1. Install mkcert (one-time)
brew install mkcert nss          # macOS
mkcert -install                  # creates a local CA trusted by your browser

# 2. Generate certificates
cd upg
bash scripts/generate-certs.sh

# 3. Done — start developing as usual
cd server && honcho start        # → https://localhost:8000
cd client && npm run dev         # → https://localhost:3000
```

Both Vite and Uvicorn auto-detect the certs. If they're missing, everything falls back to plain HTTP — nothing breaks, you just lose HTTPS.

## What the script does

`scripts/generate-certs.sh` uses [mkcert](https://github.com/FiloSottile/mkcert) to create locally-trusted certificates. It generates two pairs of files in a `certs/` directory at the project root:

```
upg/
├── certs/                       ← gitignored, generated per developer
│   ├── frontend-cert.pem        ← Vite dev server
│   ├── frontend-key.pem
│   ├── backend-cert.pem         ← Uvicorn
│   └── backend-key.pem
├── scripts/
│   └── generate-certs.sh        ← run this once
├── client/
│   └── vite.config.ts           ← reads from ../certs/
└── server/
    ├── Procfile                 ← calls scripts/run-web.sh
    └── scripts/
        └── run-web.sh           ← auto-detects certs
```

## How it works

**mkcert** creates a Certificate Authority (CA) on your machine and adds it to your system trust store and browser trust stores. Certificates signed by this CA are trusted by your browser without any warnings — unlike self-signed certs which trigger "Your connection is not private" errors.

The CA's private key never leaves your machine. Each developer generates their own CA and certificates.

## How the app uses the certs

**Frontend** (`client/vite.config.ts`): Checks if `certs/frontend-cert.pem` and `certs/frontend-key.pem` exist. If yes, Vite starts with HTTPS. If not, it falls back to HTTP.

**Backend** (`server/scripts/run-web.sh`): Called by the Procfile. Checks if `certs/backend-cert.pem` and `certs/backend-key.pem` exist. If yes, Uvicorn starts with `--ssl-keyfile` and `--ssl-certfile`. If not, plain HTTP.

Both sides detect certs independently, so if only one side has them, it still starts, though you can hit mixed-content issues in the browser.

## Installing mkcert

### macOS

```bash
brew install mkcert nss
mkcert -install
```

`nss` is required for Firefox to trust the local CA. Chrome and Safari use the system keychain directly.

### Ubuntu / Debian

```bash
sudo apt install libnss3-tools
# Install mkcert via Homebrew on Linux or download from GitHub releases
brew install mkcert
mkcert -install
```

### Windows

```bash
choco install mkcert
mkcert -install
```

Or download from [mkcert releases](https://github.com/FiloSottile/mkcert/releases).

## Troubleshooting

### Browser still shows security warning

The local CA might not be installed properly. Run:

```bash
mkcert -install
```

This adds the CA to your system trust store. Restart your browser after running this.

### "ERR_SSL_VERSION_OR_CIPHER_MISMATCH"

Regenerate the certificates — they may be corrupted or from a different CA:

```bash
rm -rf certs/
bash scripts/generate-certs.sh
```

### Firefox doesn't trust the cert

Make sure `nss` (Network Security Services) is installed:

```bash
brew install nss        # macOS
sudo apt install libnss3-tools  # Ubuntu
```

Then re-run `mkcert -install`.

### Certificates expired

mkcert certificates are valid for ~2 years by default. If they expire, just regenerate:

```bash
bash scripts/generate-certs.sh
```

### I don't want HTTPS locally

Just don't generate the certs. Both Vite and Uvicorn will fall back to plain HTTP automatically. You'll see a console warning but everything works.
