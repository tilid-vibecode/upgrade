# Upgrade (`upg`)

This repository is the current local development setup for Upgrade.

As of March 10, 2026, the codebase contains:

- A React client with four routes:
  - `/` overview
  - `/company-upload` company CSV/PDF upload
  - `/questionnaire` static questionnaire page
  - `/health` frontend health page
- A Django + FastAPI backend
- Dramatiq worker and scheduler processes
- Local MinIO storage for both processing and persistent document storage
- Local Qdrant for vector infrastructure

The upload flow that exists today accepts CSV and PDF files and stores each file in both storage buckets. The questionnaire page is UI-only for now. The later pipeline apps are scaffolded, but their business logic is not implemented yet.

## What a new developer needs to know

- Run the backend from `server/` with `honcho start`.
- Run the frontend from `client/` with `npm run dev`.
- The backend reads `.env` from the repository root, not from `server/`.
- For a clean local boot you need PostgreSQL, Redis, MinIO, and Qdrant running locally.
- If you want local HTTPS, generate certificates once with `bash scripts/generate-certs.sh`.
- The checked-in ESCO snapshot lives in `server/esco/dataset_v1.2.1` and should be imported into PostgreSQL during first-time setup.

## Recommended local stack

The commands below assume macOS with Homebrew because that matches the current team setup. Another OS is fine if you install equivalent packages and keep the same ports.

Install these first:

- Python 3.13
- Node.js 22 and npm
- PostgreSQL 17 or newer
- Redis
- MinIO
- Qdrant
- mkcert and `nss` for trusted local HTTPS certificates

### Install on macOS

```bash
brew install python@3.13 node@22 postgresql@17 redis minio/stable/minio mkcert nss
brew install qdrant/tap/qdrant
```

After that, trust the local certificate authority once:

```bash
mkcert -install
```

## First-time setup

### 1. Clone the repository and enter it

```bash
git clone <repo-url> upg
cd upg
```

### 2. Create the root environment file

The backend loads environment variables from `.env` in the repository root.

```bash
cp .env.example .env
```

The defaults in `.env.example` are already set for a normal local setup:

- PostgreSQL database: `upg`
- PostgreSQL user: `upg`
- Redis: `localhost:6379`
- MinIO API: `http://localhost:9000`
- Qdrant: `localhost:6333`
- Frontend URL: `https://localhost:3000`

You usually only need to change `.env` if:

- your local PostgreSQL username or password is different
- you start MinIO with different credentials
- you want to add `OPENAI_API_KEY`

### 3. Create the PostgreSQL role and database

Start PostgreSQL:

```bash
brew services start postgresql@17
```

Create the local user and database:

```bash
createuser upg
createdb -O upg upg
```

If `createuser upg` says the role already exists, continue.

### 4. Start Redis

```bash
brew services start redis
redis-cli ping
```

Expected result:

```text
PONG
```

### 5. Start MinIO

Create a local data directory once:

```bash
mkdir -p ~/minio-data
```

Start MinIO in a dedicated terminal:

```bash
MINIO_ROOT_USER=minioadmin MINIO_ROOT_PASSWORD=minioadmin minio server ~/minio-data --console-address ":9001"
```

Important:

- `MINIO_ROOT_USER` must match `MINIO_ACCESS_KEY` in `.env`
- `MINIO_ROOT_PASSWORD` must match `MINIO_SECRET_KEY` in `.env`

MinIO endpoints:

- API: [http://localhost:9000](http://localhost:9000)
- Console: [http://localhost:9001](http://localhost:9001)

### 6. Prepare the Python environment

```bash
cd server
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 7. Run Django bootstrap commands once

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py import_esco
python manage.py bootstrap_catalog_resolution
```

`import_esco` auto-discovers the checked-in ESCO dataset at `server/esco/dataset_v1.2.1` and loads the global ESCO catalog tables into PostgreSQL.

`bootstrap_catalog_resolution` seeds a small curated starter set of global overrides for shorthand or non-ESCO terms. Review items for unresolved skills and occupations are created automatically later during CV ingestion, self-assessment, and blueprint processing.

Optional:

```bash
python manage.py createsuperuser
```

### 8. Install client dependencies

Open a new terminal tab:

```bash
cd /path/to/upg/client
npm install
```

No client env file is required for normal local development. The Vite dev server proxies `/api` to `https://localhost:8000` by default.

If you intentionally run the backend without certificates, create `client/.env.local` with:

```bash
VITE_DEV_API_TARGET=http://localhost:8000
```

### 9. Generate local HTTPS certificates

From the repository root:

```bash
cd /path/to/upg
bash scripts/generate-certs.sh
```

This creates:

- `certs/frontend-cert.pem`
- `certs/frontend-key.pem`
- `certs/backend-cert.pem`
- `certs/backend-key.pem`

The backend and Vite dev server both auto-detect these files.

## Running the application

You need three terminals.

### Terminal 1: MinIO

If MinIO is not already running, start it:

```bash
MINIO_ROOT_USER=minioadmin MINIO_ROOT_PASSWORD=minioadmin minio server ~/minio-data --console-address ":9001"
```

### Terminal 2: backend, worker, scheduler, and Qdrant

```bash
cd /path/to/upg/server
source .venv/bin/activate
honcho start
```

What `honcho start` launches:

- `web`: Django + FastAPI on `https://localhost:8000`
- `worker`: Dramatiq worker
- `scheduler`: Django scheduler process
- `qdrant`: local Qdrant process

Qdrant lookup order:

1. `qdrant` from your shell `PATH`
2. `$HOME/qdrant`

If both are missing, the Qdrant process exits with a clear error message.

### Terminal 3: frontend

```bash
cd /path/to/upg/client
npm run dev
```

Open the app:

- Frontend: [https://localhost:3000](https://localhost:3000)
- Upload page: [https://localhost:3000/company-upload](https://localhost:3000/company-upload)
- Questionnaire page: [https://localhost:3000/questionnaire](https://localhost:3000/questionnaire)

## Quick verification checklist

After both terminals are running, verify these URLs:

- Frontend home: [https://localhost:3000](https://localhost:3000)
- Frontend health page: [https://localhost:3000/health](https://localhost:3000/health)
- Backend live check: [https://localhost:8000/api/v1/health/live](https://localhost:8000/api/v1/health/live)
- Backend ready check: [https://localhost:8000/api/v1/health/ready](https://localhost:8000/api/v1/health/ready)
- Django admin: [https://localhost:8000/admin/](https://localhost:8000/admin/)
- MinIO console: [http://localhost:9001](http://localhost:9001)
- Qdrant dashboard: [http://localhost:6333/dashboard](http://localhost:6333/dashboard)

Expected behavior:

- The backend `ready` check should report Redis and storage as healthy.
- On the upload page you should be able to select CSV or PDF files.
- Each successful upload creates one database record and writes the file into both the processing and persistent storage buckets.

## Useful local commands

### Backend only

```bash
cd server
source .venv/bin/activate
python manage.py migrate
python manage.py createsuperuser
python manage.py shell
```

### Client only

```bash
cd client
npm run dev
npm run build
```

## Troubleshooting

### `honcho start` fails immediately

Check the four required local dependencies first:

- PostgreSQL
- Redis
- MinIO
- Qdrant

The backend startup is strict about Redis and storage. If Redis or MinIO are down, the web process exits.

### The site opens on HTTP instead of HTTPS

Run:

```bash
cd /path/to/upg
bash scripts/generate-certs.sh
```

Then restart both:

- `honcho start`
- `npm run dev`

### Qdrant does not start

Install it with Homebrew:

```bash
brew install qdrant/tap/qdrant
```

Then confirm:

```bash
qdrant --version
```

### MinIO access errors

Make sure these values match:

- `.env` -> `MINIO_ACCESS_KEY`
- `.env` -> `MINIO_SECRET_KEY`
- your MinIO startup command -> `MINIO_ROOT_USER`
- your MinIO startup command -> `MINIO_ROOT_PASSWORD`

### Client cannot reach the backend

Check:

- backend is running on port `8000`
- frontend is running on port `3000`
- `client/.env.local` is not pointing to an old API URL

### Upload endpoint errors

The current first-stage intake only accepts:

- `.csv`
- `.pdf`

The upload API endpoint is:

- `POST /api/v1/company-intake/documents/upload`

The document listing endpoint is:

- `GET /api/v1/company-intake/workspaces/{workspace_slug}/documents`

## Files that matter for local development

- Root env example: [/Users/nikita/Sites/upg/.env.example](/Users/nikita/Sites/upg/.env.example)
- Backend Procfile: [/Users/nikita/Sites/upg/server/Procfile](/Users/nikita/Sites/upg/server/Procfile)
- Backend HTTPS launcher: [/Users/nikita/Sites/upg/server/scripts/run-web.sh](/Users/nikita/Sites/upg/server/scripts/run-web.sh)
- Backend Qdrant launcher: [/Users/nikita/Sites/upg/server/scripts/run-qdrant.sh](/Users/nikita/Sites/upg/server/scripts/run-qdrant.sh)
- Frontend dev config: [/Users/nikita/Sites/upg/client/vite.config.ts](/Users/nikita/Sites/upg/client/vite.config.ts)
- HTTPS certificate generator: [/Users/nikita/Sites/upg/scripts/generate-certs.sh](/Users/nikita/Sites/upg/scripts/generate-certs.sh)
