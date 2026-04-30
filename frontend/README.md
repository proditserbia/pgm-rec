# PGMRec Frontend

Minimal React + TypeScript + Vite web UI for the PGMRec backend.

## Setup

```bash
cp .env.example .env
# Edit .env and set VITE_API_BASE_URL to your backend URL
npm install
npm run dev    # http://localhost:5173
```

## Build

```bash
npm run build   # outputs to dist/
npm run preview # preview built app
```

## Environment Variables

| Variable            | Default                  | Description          |
|---------------------|--------------------------|----------------------|
| `VITE_API_BASE_URL` | `http://localhost:8000`  | Backend API base URL |

## Dev Login

Go to `/login` and enter any username. This sets `localStorage.pgmrec_authed=1`.
No real authentication — dev-mode gate only.

## Pages

| Route           | Description                                         |
|-----------------|-----------------------------------------------------|
| `/login`        | Dev login placeholder                               |
| `/`             | Dashboard — all channels, status, controls          |
| `/channels/:id` | Channel detail — config, logs, watchdog, anomalies  |
| `/exports/new`  | Create export — resolve range → track job           |
| `/exports`      | Export jobs list with filter and actions            |
