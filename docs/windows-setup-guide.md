# PGMRec — Windows Local Setup Guide

This guide walks you through installing and running PGMRec on a **Windows 10 or 11**
machine from scratch. It is written for operators, not developers — no prior Python or
programming experience is assumed.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Get the Project](#2-get-the-project)
3. [Environment Configuration](#3-environment-configuration)
4. [Database Setup](#4-database-setup)
5. [Backend Setup](#5-backend-setup)
6. [Frontend Setup](#6-frontend-setup)
7. [Access the Application](#7-access-the-application)
8. [First Test](#8-first-test)
9. [Common Issues](#9-common-issues)
10. [Optional: LAN Access](#10-optional-lan-access)

---

## 1. Prerequisites

Install each tool below **before** starting the project setup.

---

### 1.1 Python 3.12

1. Open your browser and go to:
   **https://www.python.org/downloads/release/python-3120/**
2. Scroll down and click **Windows installer (64-bit)**.
3. Run the installer.
   - **Check the box** "Add Python 3.12 to PATH" on the first screen — this is required.
   - Click **Install Now**.
4. When it finishes, click **Close**.

**Verify the install** — open a new Command Prompt (`Win + R` → type `cmd` → Enter) and run:

```cmd
python --version
```

Expected output: `Python 3.12.x`

---

### 1.2 Node.js 20 LTS

1. Go to: **https://nodejs.org/en/download**
2. Download the **Windows Installer (.msi)** for version **20 LTS**.
3. Run the installer. Leave all options at their defaults.

**Verify:**

```cmd
node --version
npm --version
```

Expected output: `v20.x.x` and `10.x.x` (or higher).

---

### 1.3 FFmpeg

FFmpeg is the video encoding engine that PGMRec uses for all recording operations.

1. Go to: **https://www.gyan.dev/ffmpeg/builds/**
2. Under the **"release builds"** section, download `ffmpeg-release-essentials.zip`.
3. Extract the ZIP file. You will get a folder like `ffmpeg-7.x.x-essentials_build`.
4. Move that folder to a permanent location, for example:
   ```
   C:\ffmpeg
   ```
   After moving, the `ffmpeg.exe` and `ffprobe.exe` binaries should be at:
   ```
   C:\ffmpeg\bin\ffmpeg.exe
   C:\ffmpeg\bin\ffprobe.exe
   ```
5. Add FFmpeg to your system PATH so it can be called from anywhere:
   - Press `Win + S`, search for **"Environment Variables"**, click
     **"Edit the system environment variables"**.
   - Click **Environment Variables…** at the bottom right.
   - Under **System variables**, find and select **Path**, then click **Edit**.
   - Click **New** and add: `C:\ffmpeg\bin`
   - Click **OK** on all dialogs.
6. Open a **new** Command Prompt window and verify:

```cmd
ffmpeg -version
ffprobe -version
```

Both commands should print version information without errors.

---

### 1.4 Git (optional but recommended)

Git lets you download and update the project from the command line.

1. Download: **https://git-scm.com/download/win**
2. Run the installer with default options.

**Verify:**

```cmd
git --version
```

---

## 2. Get the Project

Open **Command Prompt** (`cmd`) and decide where you want to store PGMRec.
A good location is `C:\PGMRec\app`.

### Option A — Clone with Git (recommended)

```cmd
mkdir C:\PGMRec
cd C:\PGMRec
git clone https://github.com/proditserbia/pgm-rec.git app
```

### Option B — Download a ZIP

1. Go to the repository page and click **Code → Download ZIP**.
2. Extract the ZIP into `C:\PGMRec\app`.

After either option your folder structure looks like this:

```
C:\PGMRec\
└── app\
    ├── backend\       ← Python API server
    ├── frontend\      ← React web UI
    ├── scripts\       ← Windows service helpers
    ├── docs\          ← Documentation
    ├── .env.example   ← Configuration template
    └── README.md
```

---

## 3. Environment Configuration

The backend reads its settings from a `.env` file in the **project root** (`C:\PGMRec\app`).

### 3.1 Create the `.env` file

Open Command Prompt, go to the project root, and copy the template:

```cmd
cd C:\PGMRec\app
copy .env.example .env
```

### 3.2 Edit `.env`

Open the file in Notepad:

```cmd
notepad .env
```

Edit the lines below. Everything not listed here can be left at its default.

---

#### `PGMREC_DATA_DIR` — where recordings, manifests and exports are stored

Uncomment and set this to a directory with plenty of disk space:

```env
PGMREC_DATA_DIR=C:\PGMRec\data
```

Create the folder now:

```cmd
mkdir C:\PGMRec\data
```

---

#### `PGMREC_FFMPEG_PATH_OVERRIDE` — path to `ffmpeg.exe`

Uncomment and set to the path you used in section 1.3:

```env
PGMREC_FFMPEG_PATH_OVERRIDE=C:\ffmpeg\bin\ffmpeg.exe
```

---

#### `PGMREC_FFPROBE_PATH` — path to `ffprobe.exe`

Uncomment and set:

```env
PGMREC_FFPROBE_PATH=C:\ffmpeg\bin\ffprobe.exe
```

---

#### `PGMREC_DATABASE_URL` — which database to use

**SQLite (simplest — no extra install needed):**

Uncomment and change to:

```env
PGMREC_DATABASE_URL=sqlite:///C:/PGMRec/data/pgmrec.db
```

**PostgreSQL (recommended for 24/7 use — see Section 4B):**

```env
PGMREC_DATABASE_URL=postgresql+psycopg://pgmrec:your-strong-password@localhost:5432/pgmrec
```

---

#### `PGMREC_ADMIN_USERNAME` and `PGMREC_ADMIN_PASSWORD`

These are the credentials for the main admin account created at first startup.

```env
PGMREC_ADMIN_USERNAME=admin
PGMREC_ADMIN_PASSWORD=change-this-to-something-strong
```

⚠️ Change the password. The default (`pgmrec-admin`) is insecure.

---

#### `PGMREC_JWT_SECRET_KEY` — token signing secret

Generate a strong random key. In Command Prompt:

```cmd
python -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output and paste it:

```env
PGMREC_JWT_SECRET_KEY=paste-the-generated-value-here
```

---

Save and close Notepad.

---

## 4. Database Setup

### Option A — SQLite (default, no install required)

SQLite requires nothing extra. The database file is created automatically at the
path you set in `PGMREC_DATABASE_URL` when you first start the backend.

**Use SQLite if:** you are testing, or running a single-channel trial.

Skip to [Section 5](#5-backend-setup).

---

### Option B — PostgreSQL (recommended for production)

PostgreSQL handles sustained 24/7 write load much better than SQLite.

#### Install PostgreSQL

1. Download the installer from: **https://www.postgresql.org/download/windows/**
2. Click **Download the installer** next to version **16** (or the latest stable).
3. Run the installer:
   - Default port: `5432` — leave it as-is.
   - Set a **superuser password** for the `postgres` account and write it down.
   - Leave all other options at their defaults.
4. When the installer asks to launch **Stack Builder**, uncheck it and click **Finish**.

#### Create the database and user

Open **pgAdmin 4** (installed with PostgreSQL) or use the **psql** command line.

**Using psql (Command Prompt):**

```cmd
"C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres
```

Enter the superuser password when prompted. Then run these three SQL commands
one at a time (replace `your-strong-password` with a real password):

```sql
CREATE USER pgmrec WITH PASSWORD 'your-strong-password';
CREATE DATABASE pgmrec OWNER pgmrec;
\q
```

#### Update `.env`

Make sure `PGMREC_DATABASE_URL` in your `.env` is set to:

```env
PGMREC_DATABASE_URL=postgresql+psycopg://pgmrec:your-strong-password@localhost:5432/pgmrec
```

#### Run migrations

After completing Section 5 (virtual environment + requirements), run:

```cmd
cd C:\PGMRec\app\backend
.venv\Scripts\activate
alembic upgrade head
```

You should see output ending with `Running upgrade  -> 0001` and `-> 0002`.

---

## 5. Backend Setup

All commands in this section are run in **Command Prompt** unless stated otherwise.

### 5.1 Go to the backend folder

```cmd
cd C:\PGMRec\app\backend
```

### 5.2 Create a Python virtual environment

```cmd
python -m venv .venv
```

This creates an isolated Python environment inside `C:\PGMRec\app\backend\.venv\`.

### 5.3 Activate the virtual environment

```cmd
.venv\Scripts\activate
```

Your prompt will change to start with `(.venv)` — this means the virtual environment
is active. You must do this every time you open a new Command Prompt to work with
the backend.

### 5.4 Install Python dependencies

```cmd
pip install -r requirements.txt
```

This takes a minute or two. It downloads and installs FastAPI, SQLAlchemy, Uvicorn,
and all other required libraries.

### 5.5 (PostgreSQL only) Run database migrations

If you chose PostgreSQL in Section 4B:

```cmd
alembic upgrade head
```

If you chose SQLite, skip this step — the database is created automatically.

### 5.6 Start the backend server

```cmd
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

You should see output similar to:

```
INFO:     Started server process [XXXX]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000
```

**Leave this Command Prompt window open.** The backend must stay running while you use PGMRec.

To stop the backend at any time, press `Ctrl + C` in this window.

---

## 6. Frontend Setup

Open a **second** Command Prompt window for the frontend (keep the backend window running).

### 6.1 Go to the frontend folder

```cmd
cd C:\PGMRec\app\frontend
```

### 6.2 Create the frontend `.env` file

```cmd
copy .env.example .env
```

The file already contains the correct default value:

```env
VITE_API_BASE_URL=http://localhost:8000
```

Leave it as-is for local use.

### 6.3 Install Node.js dependencies

```cmd
npm install
```

This takes a minute or two and downloads the React UI libraries.

### 6.4 Start the frontend dev server

```cmd
npm run dev
```

You should see:

```
  VITE v5.x.x  ready in XXX ms

  ➜  Local:   http://localhost:5173/
```

**Leave this window open as well.**

---

## 7. Access the Application

Open your browser and go to:

**http://localhost:5173**

You will see the PGMRec login page.

Log in with the credentials you set in `.env`:

| Field    | Value                         |
|----------|-------------------------------|
| Username | `admin` (or your custom name) |
| Password | The password you set          |

After logging in you will see the **Dashboard**.

---

## 8. First Test

Follow these steps to confirm everything is working correctly.

### 8.1 Open the Dashboard

The Dashboard shows all configured channels and their recording status.
Each channel card shows a status badge: **Stopped**, **Running**, or **Healthy**.

### 8.2 Start a channel recording

Click on a channel card, then click **Start**. The status should change to **Running**
within a few seconds.

### 8.3 Verify segments are being created

After 5 minutes (the default segment length) a segment file will appear under:

```
C:\PGMRec\data\channels\<channel-id>\1_record\
```

You can check this folder in Windows Explorer. MP4 files named with a timestamp
(e.g. `300425-140000.mp4`) confirm the recording is working.

### 8.4 Open the channel log

On the channel detail page, click the **Log** tab. You should see live FFmpeg
output scrolling as recording continues.

### 8.5 Create an export

1. Click **Exports → New Export**.
2. Select a channel and date.
3. Enter an in-time and out-time (e.g. `10:00:00` to `10:05:00`).
4. Click **Create Export**.
5. The job appears in the **Export Jobs** list with status **Queued** then **Running**.
6. When it reaches **Completed**, click **Download** to save the MP4 file.

---

## 9. Common Issues

---

### FFmpeg not found

**Error:** `FileNotFoundError` or `ffmpeg: command not found` in the backend log.

**Fix:**
- Confirm `ffmpeg.exe` exists at the path you set in `.env`:
  ```cmd
  dir C:\ffmpeg\bin\ffmpeg.exe
  ```
- Make sure `PGMREC_FFMPEG_PATH_OVERRIDE` in `.env` uses the full path with no typos.
- Restart the backend after any `.env` change.

---

### Port already in use

**Error:** `ERROR: [Errno 10048] error while attempting to bind on address ('127.0.0.1', 8000)`

**Fix:** Another application is using port 8000. Either:

Option 1 — Find and stop the other process:
```cmd
netstat -aon | findstr :8000
```
Note the PID in the last column, then:
```cmd
taskkill /PID <the-pid> /F
```

Option 2 — Change PGMRec to a different port. In `.env`:
```env
PGMREC_PORT=8001
```
Then start the backend with:
```cmd
uvicorn app.main:app --host 127.0.0.1 --port 8001
```
And update `frontend\.env`:
```env
VITE_API_BASE_URL=http://localhost:8001
```

---

### Database connection failed (PostgreSQL)

**Error:** `connection refused` or `password authentication failed`

**Fix checklist:**
1. Confirm PostgreSQL is running:
   ```cmd
   "C:\Program Files\PostgreSQL\16\bin\pg_isready.exe"
   ```
   Should print `localhost:5432 - accepting connections`.
2. Check the password in `PGMREC_DATABASE_URL` matches what you set when creating the user.
3. Check the database name and username are correct.
4. If you just installed PostgreSQL, you may need to run migrations first:
   ```cmd
   cd C:\PGMRec\app\backend
   .venv\Scripts\activate
   alembic upgrade head
   ```

---

### CORS error in the browser

**Error:** Browser console shows `Access to fetch at 'http://localhost:8000' has been blocked by CORS policy`

**Fix:** Open `.env` and make sure `PGMREC_CORS_ORIGINS` includes the exact origin
your browser is using:

```env
PGMREC_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000
```

Restart the backend after saving `.env`.

---

### Permission denied on data folders (Windows)

**Error:** `PermissionError: [Errno 13] Permission denied: 'C:\PGMRec\data\...'`

**Fix:**
1. Right-click `C:\PGMRec\data` in Windows Explorer.
2. Select **Properties → Security → Edit**.
3. Make sure your Windows user account has **Full control** on this folder.
4. Apply to all subfolders.

If running PGMRec as a Windows service, the service account (e.g. `LocalSystem` or
the user you chose during NSSM setup) must also have Full control on this folder.

---

### Virtual environment not activated

**Error:** `pip` or `uvicorn` commands fail with `not recognized`

**Fix:** You must activate the virtual environment before running backend commands:

```cmd
cd C:\PGMRec\app\backend
.venv\Scripts\activate
```

Your prompt should start with `(.venv)`. If it does not, the environment is not active.

---

### `.env` changes not taking effect

**Fix:** The backend reads `.env` only at startup. After any change to `.env`,
stop the backend (`Ctrl + C`) and start it again.

---

## 10. Optional: LAN Access

By default PGMRec only accepts connections from the same machine (`127.0.0.1`).
To allow other computers on your local network to reach it:

### 10.1 Find your machine's LAN IP address

```cmd
ipconfig
```

Look for the **IPv4 Address** under your active network adapter.
Example: `192.168.1.50`

### 10.2 Update `.env`

```env
PGMREC_HOST=0.0.0.0
PGMREC_PORT=8000
PGMREC_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000,http://192.168.1.50:8000
```

Replace `192.168.1.50` with your actual LAN IP.

### 10.3 Update `frontend\.env`

```env
VITE_API_BASE_URL=http://192.168.1.50:8000
```

### 10.4 Restart both backend and frontend

Stop both processes with `Ctrl + C`, then restart:

```cmd
rem --- backend window ---
cd C:\PGMRec\app\backend
.venv\Scripts\activate
uvicorn app.main:app --host 0.0.0.0 --port 8000

rem --- frontend window ---
cd C:\PGMRec\app\frontend
npm run dev
```

### 10.5 Allow port 8000 through Windows Firewall

Run this command once in an **Administrator** Command Prompt:

```cmd
netsh advfirewall firewall add rule name="PGMRec Backend" dir=in action=allow protocol=TCP localport=8000
```

### 10.6 Access from another machine

Open a browser on any machine on the same LAN and go to:

```
http://192.168.1.50:5173
```

(Replace `192.168.1.50` with your server's actual IP.)

---

## Quick Reference — Starting PGMRec After Setup

After initial setup, this is all you need to do each time:

**Window 1 — Backend:**

```cmd
cd C:\PGMRec\app\backend
.venv\Scripts\activate
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**Window 2 — Frontend:**

```cmd
cd C:\PGMRec\app\frontend
npm run dev
```

Then open **http://localhost:5173** in your browser.

---

> **Tip — Running as a Windows service (unattended startup)**
>
> If you want PGMRec to start automatically when Windows boots (without logging in),
> see the Windows service scripts in `scripts\windows\`. They use
> [NSSM](https://nssm.cc/) to register the backend as a Windows service.
> Read `scripts\windows\install_service.ps1` for instructions.
