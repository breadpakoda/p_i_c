# PromptGuard

PromptGuard is a React/Vite chat client backed by a Node API, MongoDB, and a
FastAPI AI engine.

## Run on Windows

Prerequisites: MongoDB listening on port `27017`, Node.js/npm, Python with the
AI engine dependencies and model assets installed, and a configured root `.env`
file.

Run the app from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

The script opens separate PowerShell windows for the Node API, FastAPI AI
engine, and Vite client. If the Windows MongoDB service exists, it starts it
when needed; otherwise, start MongoDB yourself before running the script.
Close those service windows to stop the app.

Open [http://localhost:5173](http://localhost:5173). Health checks are available
at `http://localhost:5000/health` and `http://localhost:8000/health`.

## Docker deployment

Create the deployment environment file and replace every placeholder:

```powershell
Copy-Item .env.example .env
docker compose up --build -d
```

Open [http://localhost](http://localhost). The compose stack provides MongoDB,
the Node API, the FastAPI AI engine, and the production Vite/Nginx client.
Container health endpoints are:

- `http://localhost/` for the client
- `http://localhost:5000/health` for MongoDB-backed API readiness
- `http://localhost:8000/health` for the AI engine

Before deployment, configure real LLM credentials and generate separate random
values for `INTERNAL_SERVICE_TOKEN` and `JWT_SECRET`. Keep `.env` out of source
control. Persist MongoDB data in the `mongo-data` volume.

The AI engine stores its conversation history and Layer 3 security status via
the Node API in MongoDB. Keep the Node API and MongoDB running while using the
chat service.

## Tests

Run the Python unit and detector-corpus tests from the root:

```powershell
python -m pytest -q
```

The real detector corpus requires the Layer 1 and Layer 2 model assets. For
server integration tests and browser E2E tests, start the services first; see
[runtime-validation-report.md](./runtime-validation-report.md) for the commands
and tested flows.
