# TinyLink

TinyLink is a URL shortener that converts long URLs into short, memorable codes. Users can redirect to the original URL by visiting the short code in their browser. It is built as a production-style REST API with authentication, caching, asynchronous analytics, and rate limiting.

## Tech Stack

* **Python + FastAPI** — for automatic request validation using type hints and auto-generated Swagger/OpenAPI documentation.
* **PostgreSQL + SQLAlchemy** — for persistent relational data storage and ORM-based database access.
* **Redis (Memurai)** — for in-memory caching of frequently accessed redirects and storing rate-limiting counters.
* **Celery** — for processing analytics writes asynchronously in a background worker.
* **JWT + bcrypt** — for stateless authentication and secure password hashing.
* **Docker** — for containerized database environments and consistent setup. Native PostgreSQL was used during development due to WSL2 limitations.

## Architecture Decisions

### 1. Base62 Encoding Over Random Codes

TinyLink uses Base62 encoding of the database-generated link ID instead of generating random short codes.

Random codes can eventually produce collisions as the number of links grows, which means the application needs to detect collisions and generate another code. With Base62 encoding, the short code is derived from an auto-incrementing primary key. Since the database guarantees that each ID is unique, the resulting short codes are also unique. This eliminates the need for collision detection and retry logic.

### 2. Hot Path / Cold Path Split

The redirect endpoint is the application's hot path because the user is actively waiting for the redirect to happen. Analytics writes, on the other hand, do not need to happen before the redirect is returned.

If analytics were written synchronously as part of the redirect request, a slow database operation could delay every redirect. Instead, TinyLink queues the analytics operation with Celery. The FastAPI process can return the redirect response immediately while a separate Celery worker records the click in the background.

This also provides process-level isolation: the Celery worker runs as a separate Python process from FastAPI and therefore has its own Python interpreter and GIL, allowing the two processes to execute independently.

### 3. Redis Cache-Aside Pattern

The redirect endpoint uses a cache-aside strategy.

On each redirect, TinyLink checks Redis first. If the short code is cached, the original URL can be returned without querying PostgreSQL. On a cache miss, the application queries PostgreSQL, stores the result in Redis with a 24-hour TTL, and then returns the URL.

This means frequently accessed links remain in the fast in-memory cache, while less frequently accessed links fall through to PostgreSQL when needed.

### 4. Atomic Rate Limiting with a Lua Script

Rate limiting requires two Redis operations: incrementing a request counter and setting its expiration time.

If these operations were separate, the application could theoretically execute the increment and then fail before setting the expiry. The counter would then remain in Redis without a TTL, potentially causing the user to remain rate limited indefinitely.

TinyLink uses a Redis Lua script to execute both operations atomically. Redis executes the entire script as one operation, so there is no gap between incrementing the counter and setting its expiration.

## How to Run

### 1. Clone the repository

```bash
git clone <your-repo-url>
cd tinylink
```

### 2. Create and activate a virtual environment

On Windows:

```bash
python -m venv venv
.\venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file from the example:

```bash
cp .env.example .env
```

Then edit `.env` with your local database credentials and secret key.

### 5. Start the required services

Start PostgreSQL using either your native installation or Docker.

Start Redis using Memurai on Windows.

### 6. Create database tables

Database tables are created automatically when the application starts using SQLAlchemy.

### 7. Start the FastAPI server

```bash
uvicorn app.main:app --reload
```

The API will be available locally, and FastAPI's interactive Swagger documentation can be accessed at:

```text
http://127.0.0.1:8000/docs
```

### 8. Start the Celery worker

Open a separate terminal, activate the virtual environment, and run:

```bash
celery -A app.tasks worker --loglevel=info --pool=solo
```

## API Endpoints

| Method | Endpoint                     | Auth Required | Description                           |
| ------ | ---------------------------- | ------------- | ------------------------------------- |
| POST   | `/api/v1/auth/register`      | No            | Create an account and receive a JWT   |
| POST   | `/api/v1/auth/login`         | No            | Authenticate and receive a JWT        |
| POST   | `/api/v1/links`              | Yes           | Create a short link                   |
| GET    | `/api/v1/links`              | Yes           | List the user's links with pagination |
| GET    | `/api/v1/{code}`             | No            | Redirect to the original URL          |
| GET    | `/api/v1/links/{code}/stats` | No            | Retrieve click analytics for a link   |

## Environment Variables

Create a `.env` file locally:

```env
DATABASE_URL=postgresql://user:password@localhost:5432/tinylink
SECRET_KEY=your-secret-key
```

For the repository, only commit the template:

```env
DATABASE_URL=postgresql://user:password@localhost:5432/tinylink
SECRET_KEY=your-secret-key-here
```

The actual `.env` file is excluded from Git using `.gitignore`.

## Project Structure

```text
tinylink/
├── app/
│   ├── main.py
│   ├── models.py
│   ├── schemas.py
│   ├── database.py
│   ├── auth.py
│   └── tasks.py
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## Git Setup

If this is a new repository:

```bash
git init
git add .
git commit -m "Initial commit - TinyLink URL shortener"
git branch -M main
git remote add origin <your-repo-url>
git push -u origin main
```
