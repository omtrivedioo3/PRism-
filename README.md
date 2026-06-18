# PR Review Agent — Complete Project Specification
### For AI-Assisted Development (Jettski / Cursor / Copilot)

> **Read this entire file before writing any code.**
> This document is the single source of truth. Every folder, file, function, environment variable, and API call is defined here. Build exactly what is described.

---

## 1. Project Overview

### What this system does

When a developer opens or updates a Pull Request on GitHub, this system automatically:
1. Receives the event via a GitHub Webhook
2. Fetches the full code diff from the GitHub API
3. Splits the diff into per-file chunks
4. Sends each chunk to Gemini (via LangChain) for AI code review
5. Aggregates all file reviews into one structured PR-level summary
6. Posts the summary as a formatted Markdown comment on the GitHub PR
7. Saves the review to PostgreSQL for history and analytics
8. Exposes a REST API so anyone can query past reviews

### What it does NOT do
- No Jira integration
- No authentication system (no user login)
- No paid services — everything runs on free tiers
- No frontend (optional simple dashboard at the end)

### Free tier strategy
| Service | Free tier used |
|---------|---------------|
| Google Cloud Run | 2 million requests/month free |
| Google Cloud Pub/Sub | 10 GB/month free |
| Gemini API (Google AI Studio) | 15 RPM, 1M tokens/day free |
| PostgreSQL | Use Supabase free tier (500MB) or Railway free tier |
| Redis | Use Upstash free tier (10,000 requests/day) |
| GitHub | Free webhooks |

---

## 2. Exact Folder Structure

Create this exact folder/file structure. Do not add extra files unless specified.

```
pr-review-agent/
├── README.md
├── .env.example
├── .gitignore
├── docker-compose.yml               ← for local development only
│
├── services/
│   │
│   ├── webhook/                     ← SERVICE 1: receives GitHub webhooks
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   ├── main.py                  ← FastAPI app, single entry point
│   │   ├── routes/
│   │   │   └── github.py            ← POST /webhook/github route
│   │   ├── utils/
│   │   │   ├── signature.py         ← verify GitHub webhook signature
│   │   │   └── pubsub.py            ← publish event to Pub/Sub
│   │   └── models/
│   │       └── events.py            ← Pydantic models for webhook payloads
│   │
│   ├── reviewer/                    ← SERVICE 2: AI review engine
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   ├── main.py                  ← subscribes to Pub/Sub, runs review pipeline
│   │   ├── pipeline/
│   │   │   ├── fetch_diff.py        ← fetch PR diff from GitHub API
│   │   │   ├── chunk_diff.py        ← split diff into per-file chunks
│   │   │   ├── review_chain.py      ← LangChain + Gemini review logic
│   │   │   ├── aggregate.py         ← merge file reviews into PR summary
│   │   │   └── post_comment.py      ← post summary to GitHub PR
│   │   ├── utils/
│   │   │   ├── cache.py             ← Redis cache for LLM results
│   │   │   └── db.py                ← PostgreSQL connection + queries
│   │   └── models/
│   │       └── review.py            ← Pydantic models for review output
│   │
│   └── api/                         ← SERVICE 3: REST API for review history
│       ├── Dockerfile
│       ├── package.json
│       ├── src/
│       │   ├── index.js             ← Express app entry point
│       │   ├── routes/
│       │   │   ├── reviews.js       ← GET /reviews, GET /reviews/:id
│       │   │   └── repos.js         ← GET /repos/:owner/:repo/stats
│       │   ├── db/
│       │   │   └── postgres.js      ← pg connection pool
│       │   └── middleware/
│       │       └── errorHandler.js
│       └── .env.example
│
├── infra/
│   ├── cloudbuild.yaml              ← GCP Cloud Build config
│   ├── pubsub-setup.sh              ← create Pub/Sub topic + subscription
│   └── deploy.sh                   ← deploy all services to Cloud Run
│
└── database/
    ├── schema.sql                   ← all CREATE TABLE statements
    └── migrations/
        └── 001_initial.sql          ← same as schema.sql, versioned
```

---

## 3. Environment Variables

### Root `.env.example` (copy to `.env` for local dev)

```env
# GitHub
GITHUB_WEBHOOK_SECRET=your_webhook_secret_here
GITHUB_TOKEN=ghp_your_personal_access_token

# Google Cloud
GCP_PROJECT_ID=your-gcp-project-id
PUBSUB_TOPIC_ID=pr-events
PUBSUB_SUBSCRIPTION_ID=pr-events-sub

# Gemini (get free key from https://aistudio.google.com)
GEMINI_API_KEY=your_gemini_api_key

# PostgreSQL (Supabase or Railway connection string)
DATABASE_URL=postgresql://user:password@host:5432/pr_reviews

# Redis (Upstash - free tier)
REDIS_URL=redis://default:password@your-upstash-url:6379

# Service ports (local dev)
WEBHOOK_PORT=8001
API_PORT=8002
```

---

## 4. Database Schema

### `database/schema.sql`

```sql
-- Run this once to set up the database

CREATE TABLE IF NOT EXISTS repositories (
  id          SERIAL PRIMARY KEY,
  owner       VARCHAR(255) NOT NULL,
  repo        VARCHAR(255) NOT NULL,
  full_name   VARCHAR(511) GENERATED ALWAYS AS (owner || '/' || repo) STORED,
  created_at  TIMESTAMP DEFAULT NOW(),
  UNIQUE(owner, repo)
);

CREATE TABLE IF NOT EXISTS pull_request_reviews (
  id                SERIAL PRIMARY KEY,
  repo_id           INTEGER REFERENCES repositories(id),
  pr_number         INTEGER NOT NULL,
  pr_title          VARCHAR(1000),
  pr_author         VARCHAR(255),
  pr_url            VARCHAR(1000),
  head_sha          VARCHAR(40) NOT NULL,        -- commit SHA, used as cache key
  overall_summary   TEXT,                         -- plain English: what this PR does
  risk_level        VARCHAR(10),                  -- 'low', 'medium', 'high'
  risk_score        INTEGER,                      -- 0-100
  files_changed     INTEGER,
  lines_added       INTEGER,
  lines_removed     INTEGER,
  github_comment_id BIGINT,                       -- ID of the posted GitHub comment
  processing_time_ms INTEGER,
  created_at        TIMESTAMP DEFAULT NOW(),
  UNIQUE(repo_id, pr_number, head_sha)            -- don't re-review same commit
);

CREATE TABLE IF NOT EXISTS file_reviews (
  id              SERIAL PRIMARY KEY,
  review_id       INTEGER REFERENCES pull_request_reviews(id) ON DELETE CASCADE,
  filename        VARCHAR(500) NOT NULL,
  language        VARCHAR(50),
  lines_added     INTEGER,
  lines_removed   INTEGER,
  file_summary    TEXT,                           -- one sentence: what changed in this file
  issues          JSONB DEFAULT '[]',             -- array of issue objects (see below)
  created_at      TIMESTAMP DEFAULT NOW()
);

-- issues JSONB structure (array of):
-- {
--   "severity": "high" | "medium" | "low" | "info",
--   "type": "security" | "bug" | "performance" | "style" | "missing_tests",
--   "line": 42,          (optional)
--   "description": "Retry loop has no timeout cap"
-- }

CREATE INDEX idx_reviews_repo_pr ON pull_request_reviews(repo_id, pr_number);
CREATE INDEX idx_reviews_created ON pull_request_reviews(created_at DESC);
CREATE INDEX idx_file_reviews_review ON file_reviews(review_id);
```

---

## 5. Service 1 — Webhook Receiver (Python / FastAPI)

### Purpose
Receive POST requests from GitHub whenever a PR is opened, updated, or closed. Verify the request is genuinely from GitHub. Publish the event to Pub/Sub so the reviewer service can pick it up asynchronously.

### `services/webhook/requirements.txt`
```
fastapi==0.111.0
uvicorn==0.30.1
pydantic==2.7.1
google-cloud-pubsub==2.21.1
httpx==0.27.0
python-dotenv==1.0.1
```

### `services/webhook/main.py`
```python
from fastapi import FastAPI
from routes.github import router as github_router
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="PR Review Agent - Webhook Service")
app.include_router(github_router, prefix="/webhook")

@app.get("/health")
def health():
    return {"status": "ok", "service": "webhook"}
```

### `services/webhook/models/events.py`
```python
from pydantic import BaseModel
from typing import Optional

class PullRequestEvent(BaseModel):
    action: str                    # 'opened', 'synchronize', 'reopened', 'closed'
    pr_number: int
    pr_title: str
    pr_author: str
    pr_url: str
    head_sha: str
    base_branch: str
    head_branch: str
    repo_owner: str
    repo_name: str
    diff_url: str
    additions: int
    deletions: int
    changed_files: int
```

### `services/webhook/utils/signature.py`
```python
import hmac
import hashlib
import os

def verify_github_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """
    GitHub sends X-Hub-Signature-256: sha256=<hash>
    We verify by computing HMAC-SHA256 of the raw payload body
    using GITHUB_WEBHOOK_SECRET as the key.
    Returns True if valid, False if tampered or missing.
    """
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if not secret or not signature_header:
        return False

    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header)
```

### `services/webhook/utils/pubsub.py`
```python
import json
import os
from google.cloud import pubsub_v1

publisher = pubsub_v1.PublisherClient()

def publish_pr_event(event_data: dict) -> str:
    """
    Publish a PR event dict to GCP Pub/Sub.
    Returns the published message ID.
    topic path format: projects/{project}/topics/{topic}
    """
    project_id = os.getenv("GCP_PROJECT_ID")
    topic_id = os.getenv("PUBSUB_TOPIC_ID", "pr-events")
    topic_path = publisher.topic_path(project_id, topic_id)

    data = json.dumps(event_data).encode("utf-8")
    future = publisher.publish(topic_path, data)
    return future.result()   # blocks until published, returns message_id
```

### `services/webhook/routes/github.py`

```python
import json
from fastapi import APIRouter, Request, HTTPException, Header
from typing import Optional
from utils.signature import verify_github_signature
from utils.pubsub import publish_pr_event
from models.events import PullRequestEvent

router = APIRouter()

# Only process these PR actions. Ignore others (labeled, assigned, etc.)
RELEVANT_ACTIONS = {"opened", "synchronize", "reopened"}

@router.post("/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None),
    x_github_event: Optional[str] = Header(None),
):
    payload_bytes = await request.body()

    # Step 1: Verify signature
    if not verify_github_signature(payload_bytes, x_hub_signature_256 or ""):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # Step 2: Only handle pull_request events
    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": f"event type '{x_github_event}' not handled"}

    payload = json.loads(payload_bytes)
    action = payload.get("action")

    # Step 3: Only handle relevant actions
    if action not in RELEVANT_ACTIONS:
        return {"status": "ignored", "reason": f"action '{action}' not relevant"}

    pr = payload["pull_request"]
    repo = payload["repository"]

    # Step 4: Build clean event object
    event = PullRequestEvent(
        action=action,
        pr_number=pr["number"],
        pr_title=pr["title"],
        pr_author=pr["user"]["login"],
        pr_url=pr["html_url"],
        head_sha=pr["head"]["sha"],
        base_branch=pr["base"]["ref"],
        head_branch=pr["head"]["ref"],
        repo_owner=repo["owner"]["login"],
        repo_name=repo["name"],
        diff_url=pr["diff_url"],
        additions=pr["additions"],
        deletions=pr["deletions"],
        changed_files=pr["changed_files"],
    )

    # Step 5: Publish to Pub/Sub and return immediately
    # Do NOT wait for review to complete — that's async
    message_id = publish_pr_event(event.model_dump())

    return {
        "status": "accepted",
        "message_id": message_id,
        "pr": f"{event.repo_owner}/{event.repo_name}#{event.pr_number}"
    }
```

### `services/webhook/Dockerfile`
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
```

---

## 6. Service 2 — AI Reviewer (Python / LangChain / Gemini)

### Purpose
Subscribe to Pub/Sub. For each PR event received: fetch the diff, chunk it by file, run LangChain+Gemini review on each chunk, aggregate results, post to GitHub, save to PostgreSQL.

### `services/reviewer/requirements.txt`
```
langchain==0.2.5
langchain-google-genai==1.0.6
google-cloud-pubsub==2.21.1
pydantic==2.7.1
psycopg2-binary==2.9.9
redis==5.0.4
httpx==0.27.0
python-dotenv==1.0.1
tenacity==8.3.0
```

### `services/reviewer/models/review.py`

These are the Pydantic models that define the exact JSON structure Gemini must return.

```python
from pydantic import BaseModel, Field
from typing import List, Optional
from enum import Enum

class Severity(str, Enum):
    HIGH   = "high"
    MEDIUM = "medium"
    LOW    = "low"
    INFO   = "info"

class IssueType(str, Enum):
    SECURITY       = "security"
    BUG            = "bug"
    PERFORMANCE    = "performance"
    STYLE          = "style"
    MISSING_TESTS  = "missing_tests"

class Issue(BaseModel):
    severity: Severity
    type: IssueType
    description: str = Field(description="One sentence describing the issue clearly")
    line: Optional[int] = Field(None, description="Line number in the diff, if identifiable")

class FileReview(BaseModel):
    filename: str
    language: str = Field(description="Programming language: python, javascript, go, etc.")
    file_summary: str = Field(description="One sentence: what changed in this file")
    issues: List[Issue] = Field(default=[], description="List of issues found. Empty list if none.")

class PRSummary(BaseModel):
    overall_summary: str = Field(description="2-3 sentences in plain English: what this PR does and why")
    risk_level: str       = Field(description="Must be exactly one of: low, medium, high")
    risk_score: int       = Field(description="Integer 0-100. 0=perfectly clean, 100=do not merge")
    key_concerns: List[str] = Field(description="Top 3 concerns across all files. Empty list if none.")
    positive_notes: List[str] = Field(description="1-2 things done well. Be specific.")
```

### `services/reviewer/pipeline/fetch_diff.py`

```python
import httpx
import os
from typing import List, Dict

GITHUB_API = "https://api.github.com"

def get_headers() -> dict:
    token = os.getenv("GITHUB_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def fetch_pr_files(owner: str, repo: str, pr_number: int) -> List[Dict]:
    """
    Fetch list of files changed in a PR via GitHub API.
    Returns list of dicts with: filename, status, additions, deletions, patch (the diff text)
    Only returns files that have a patch (i.e. have actual code changes).
    Skips binary files, renamed-only files, and files with no patch.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/files"
    params = {"per_page": 100}  # max 100 files per request

    with httpx.Client(headers=get_headers(), timeout=30) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        files = response.json()

    # Filter to only files with actual diff content
    return [
        {
            "filename": f["filename"],
            "status": f["status"],        # added, modified, removed, renamed
            "additions": f["additions"],
            "deletions": f["deletions"],
            "patch": f.get("patch", ""),  # the actual diff text — may be absent for binaries
        }
        for f in files
        if f.get("patch")  # skip files without a diff patch
    ]
```

### `services/reviewer/pipeline/chunk_diff.py`

```python
from typing import List, Dict

# Gemini free tier context window is large (1M tokens),
# but we still chunk by file for cleaner, focused reviews.
# Hard limit: skip any single file patch larger than this many characters.
MAX_PATCH_CHARS = 12000   # ~3000 tokens, safe for Gemini

# File extensions to completely skip (not worth reviewing)
SKIP_EXTENSIONS = {
    ".lock", ".sum", ".mod",           # dependency locks
    ".png", ".jpg", ".jpeg", ".gif",   # images
    ".svg", ".ico", ".woff", ".ttf",   # assets
    ".min.js", ".min.css",             # minified files
}

def should_skip_file(filename: str) -> bool:
    """Return True if we should skip reviewing this file."""
    lower = filename.lower()
    for ext in SKIP_EXTENSIONS:
        if lower.endswith(ext):
            return True
    # Skip generated/vendor files
    skip_paths = ["vendor/", "node_modules/", "__pycache__/", ".terraform/"]
    return any(p in lower for p in skip_paths)

def detect_language(filename: str) -> str:
    """Detect programming language from file extension."""
    ext_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".go": "go", ".java": "java", ".cs": "csharp",
        ".rb": "ruby", ".rs": "rust", ".cpp": "cpp", ".c": "c",
        ".sh": "bash", ".yaml": "yaml", ".yml": "yaml",
        ".json": "json", ".sql": "sql", ".md": "markdown",
        ".tf": "terraform", ".html": "html", ".css": "css",
    }
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext_map.get(suffix, "unknown")

def chunk_diff(files: List[Dict]) -> List[Dict]:
    """
    Filter and enrich the list of changed files.
    Returns only files we should review, with language detected.
    Files with patches larger than MAX_PATCH_CHARS are truncated with a note.
    """
    chunks = []
    for f in files:
        if should_skip_file(f["filename"]):
            continue

        patch = f["patch"]
        truncated = False

        if len(patch) > MAX_PATCH_CHARS:
            patch = patch[:MAX_PATCH_CHARS]
            truncated = True

        chunks.append({
            "filename": f["filename"],
            "language": detect_language(f["filename"]),
            "status": f["status"],
            "additions": f["additions"],
            "deletions": f["deletions"],
            "patch": patch,
            "truncated": truncated,
        })

    return chunks
```

### `services/reviewer/pipeline/review_chain.py`

This is the core AI logic. Read carefully.

```python
import os
import json
from typing import List, Dict
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import ChatPromptTemplate
from langchain.output_parsers import PydanticOutputParser
from tenacity import retry, stop_after_attempt, wait_exponential
from models.review import FileReview, PRSummary

# ── LLM setup ──────────────────────────────────────────────────────────────
# Use gemini-1.5-flash (free tier, fast). NOT gemini-pro (slower, lower free quota)
llm = ChatGoogleGenerativeAI(
    model="gemini-1.5-flash",
    google_api_key=os.getenv("GEMINI_API_KEY"),
    temperature=0.1,   # low temperature = more consistent, structured output
)

# ── Output parsers ──────────────────────────────────────────────────────────
file_review_parser = PydanticOutputParser(pydantic_object=FileReview)
pr_summary_parser  = PydanticOutputParser(pydantic_object=PRSummary)

# ── Prompt: per-file review ─────────────────────────────────────────────────
FILE_REVIEW_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You are a senior software engineer performing a code review.
You review code diffs carefully and provide structured, actionable feedback.
Be concise. Focus only on real issues — do not invent problems that aren't there.
Respond ONLY with valid JSON matching the requested format. No markdown, no explanation."""),
    ("human", """Review this code diff for the file: {filename}
Language: {language}
Status: {status} (added/modified/removed)

DIFF:
{patch}

{format_instructions}

Rules:
- file_summary: one sentence describing what changed (not what the code does generally)
- issues: only include REAL issues visible in the diff. Empty array if the code looks fine.
- For severity HIGH: security vulnerabilities, data loss risks, crashes
- For severity MEDIUM: bugs, missing error handling, breaking changes  
- For severity LOW: performance concerns, code smell
- For INFO: suggestions, missing tests, style notes
"""),
])

# ── Prompt: PR-level aggregation ────────────────────────────────────────────
PR_SUMMARY_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """You are a senior tech lead writing a PR summary for a manager or reviewer.
The manager is non-technical. Write clearly. Be honest about risks.
Respond ONLY with valid JSON. No markdown, no explanation outside the JSON."""),
    ("human", """Summarise this Pull Request based on the per-file reviews below.

PR Title: {pr_title}
Author: {pr_author}
Files changed: {files_changed}
Total additions: {additions} | Total deletions: {deletions}

FILE REVIEWS:
{file_reviews_json}

{format_instructions}

Rules:
- overall_summary: plain English, what the PR achieves and why it matters. 2-3 sentences max.
- risk_level: "low" if no high/medium issues. "medium" if 1-2 medium issues. "high" if any high issues.
- risk_score: 0=perfect, 100=do not merge. Scale with severity and count of issues.
- key_concerns: top 3 issues across all files. Empty array if none.
- positive_notes: 1-2 specific things done well. Be genuine, not generic.
"""),
])

# ── Chains ──────────────────────────────────────────────────────────────────
file_review_chain = FILE_REVIEW_PROMPT | llm | file_review_parser
pr_summary_chain  = PR_SUMMARY_PROMPT  | llm | pr_summary_parser

# ── Functions ────────────────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def review_single_file(chunk: Dict) -> FileReview:
    """
    Run LangChain chain to review a single file diff.
    Retries up to 3 times on failure (handles Gemini rate limits gracefully).
    """
    return file_review_chain.invoke({
        "filename": chunk["filename"],
        "language": chunk["language"],
        "status": chunk["status"],
        "patch": chunk["patch"],
        "format_instructions": file_review_parser.get_format_instructions(),
    })

def review_all_files(chunks: List[Dict]) -> List[FileReview]:
    """
    Review each file chunk sequentially.
    Sequential (not parallel) to respect Gemini free tier rate limits (15 RPM).
    Returns list of FileReview objects. Skips files that fail after retries.
    """
    results = []
    for chunk in chunks:
        try:
            review = review_single_file(chunk)
            results.append(review)
            print(f"  ✓ reviewed {chunk['filename']}")
        except Exception as e:
            print(f"  ✗ failed to review {chunk['filename']}: {e}")
            # Create a minimal review so we don't block the whole pipeline
            results.append(FileReview(
                filename=chunk["filename"],
                language=chunk["language"],
                file_summary="Review failed — could not process this file.",
                issues=[]
            ))
    return results

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def generate_pr_summary(
    file_reviews: List[FileReview],
    pr_title: str,
    pr_author: str,
    files_changed: int,
    additions: int,
    deletions: int,
) -> PRSummary:
    """
    Run a second LangChain call to aggregate all file reviews into one PR summary.
    """
    file_reviews_json = json.dumps(
        [r.model_dump() for r in file_reviews],
        indent=2
    )
    return pr_summary_chain.invoke({
        "pr_title": pr_title,
        "pr_author": pr_author,
        "files_changed": files_changed,
        "additions": additions,
        "deletions": deletions,
        "file_reviews_json": file_reviews_json,
        "format_instructions": pr_summary_parser.get_format_instructions(),
    })
```

### `services/reviewer/pipeline/aggregate.py`

```python
from typing import List
from models.review import FileReview, PRSummary

def build_markdown_comment(
    pr_summary: PRSummary,
    file_reviews: List[FileReview],
    pr_number: int,
    processing_time_ms: int,
) -> str:
    """
    Build a formatted GitHub Markdown comment from the review results.
    This is what the manager/reviewer sees on the PR page.
    """

    # Risk level emoji
    risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(pr_summary.risk_level, "⚪")

    # Severity emoji
    def sev_badge(severity: str) -> str:
        return {"high": "🔴 High", "medium": "🟡 Medium", "low": "🟠 Low", "info": "🔵 Info"}.get(severity, severity)

    lines = []

    # Header
    lines.append("## 🤖 AI Code Review")
    lines.append("")
    lines.append(f"**Risk Level:** {risk_emoji} {pr_summary.risk_level.capitalize()} ({pr_summary.risk_score}/100)")
    lines.append("")

    # Overall summary
    lines.append("### 📋 What this PR does")
    lines.append(pr_summary.overall_summary)
    lines.append("")

    # Key concerns
    if pr_summary.key_concerns:
        lines.append("### ⚠️ Key Concerns")
        for concern in pr_summary.key_concerns:
            lines.append(f"- {concern}")
        lines.append("")

    # Positive notes
    if pr_summary.positive_notes:
        lines.append("### ✅ Looks Good")
        for note in pr_summary.positive_notes:
            lines.append(f"- {note}")
        lines.append("")

    # Per-file breakdown
    lines.append("### 📁 File Breakdown")
    lines.append("")

    for review in file_reviews:
        issues_count = len(review.issues)
        icon = "✅" if issues_count == 0 else ("🔴" if any(i.severity == "high" for i in review.issues) else "🟡")
        lines.append(f"<details>")
        lines.append(f"<summary>{icon} <code>{review.filename}</code> — {review.file_summary}</summary>")
        lines.append("")

        if review.issues:
            for issue in review.issues:
                line_ref = f" (line {issue.line})" if issue.line else ""
                lines.append(f"- **{sev_badge(issue.severity)}**{line_ref}: {issue.description}")
        else:
            lines.append("No issues found.")

        lines.append("")
        lines.append("</details>")
        lines.append("")

    # Footer
    lines.append("---")
    lines.append(f"*Generated by PR Review Agent in {processing_time_ms}ms · [View all reviews](https://your-api-url/reviews)*")

    return "\n".join(lines)
```

### `services/reviewer/pipeline/post_comment.py`

```python
import os
import httpx

GITHUB_API = "https://api.github.com"

def get_headers() -> dict:
    token = os.getenv("GITHUB_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

COMMENT_MARKER = "<!-- pr-review-agent -->"

def post_or_update_comment(
    owner: str,
    repo: str,
    pr_number: int,
    body: str,
) -> int:
    """
    Post the review as a PR comment.
    If the bot has already commented on this PR, EDIT the existing comment.
    If not, CREATE a new comment.
    Returns the GitHub comment ID.
    We identify our own comments by looking for COMMENT_MARKER in the body.
    """
    full_body = f"{COMMENT_MARKER}\n{body}"

    with httpx.Client(headers=get_headers(), timeout=30) as client:
        # Check if we already have a comment on this PR
        comments_url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        resp = client.get(comments_url, params={"per_page": 100})
        resp.raise_for_status()
        comments = resp.json()

        existing_comment_id = None
        for comment in comments:
            if COMMENT_MARKER in comment.get("body", ""):
                existing_comment_id = comment["id"]
                break

        if existing_comment_id:
            # Edit existing comment
            url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/comments/{existing_comment_id}"
            resp = client.patch(url, json={"body": full_body})
            resp.raise_for_status()
            return existing_comment_id
        else:
            # Create new comment
            resp = client.post(comments_url, json={"body": full_body})
            resp.raise_for_status()
            return resp.json()["id"]
```

### `services/reviewer/utils/cache.py`

```python
import redis
import json
import os
import hashlib

_client = None

def get_redis():
    global _client
    if _client is None:
        _client = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))
    return _client

def make_cache_key(owner: str, repo: str, pr_number: int, head_sha: str) -> str:
    """Cache key based on repo + PR + exact commit SHA. Same commit = same diff = reuse cache."""
    raw = f"{owner}/{repo}/pr/{pr_number}/{head_sha}"
    return "pr_review:" + hashlib.md5(raw.encode()).hexdigest()

def get_cached_review(key: str) -> dict | None:
    """Return cached review dict or None if not cached."""
    try:
        val = get_redis().get(key)
        return json.loads(val) if val else None
    except Exception:
        return None   # Redis unavailable = just skip cache

def cache_review(key: str, data: dict, ttl_seconds: int = 86400) -> None:
    """Cache review for 24 hours. Silently fails if Redis is unavailable."""
    try:
        get_redis().setex(key, ttl_seconds, json.dumps(data))
    except Exception:
        pass
```

### `services/reviewer/utils/db.py`

```python
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

def get_connection():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

@contextmanager
def get_cursor():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def upsert_repository(owner: str, repo: str) -> int:
    """Insert repo if not exists. Return repo ID either way."""
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO repositories (owner, repo)
            VALUES (%s, %s)
            ON CONFLICT (owner, repo) DO UPDATE SET owner = EXCLUDED.owner
            RETURNING id
        """, (owner, repo))
        return cur.fetchone()["id"]

def save_review(repo_id: int, event: dict, pr_summary, file_reviews: list, comment_id: int, processing_ms: int) -> int:
    """Save the full review to the database. Returns the new review ID."""
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO pull_request_reviews
              (repo_id, pr_number, pr_title, pr_author, pr_url, head_sha,
               overall_summary, risk_level, risk_score, files_changed,
               lines_added, lines_removed, github_comment_id, processing_time_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (repo_id, pr_number, head_sha) DO NOTHING
            RETURNING id
        """, (
            repo_id, event["pr_number"], event["pr_title"], event["pr_author"],
            event["pr_url"], event["head_sha"], pr_summary.overall_summary,
            pr_summary.risk_level, pr_summary.risk_score, event["changed_files"],
            event["additions"], event["deletions"], comment_id, processing_ms,
        ))
        row = cur.fetchone()
        if not row:
            return -1   # already exists (duplicate event)
        review_id = row["id"]

        # Save individual file reviews
        import json
        for fr in file_reviews:
            cur.execute("""
                INSERT INTO file_reviews (review_id, filename, language, lines_added, lines_removed, file_summary, issues)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (
                review_id, fr.filename, fr.language,
                next((f["additions"] for f in [] if f["filename"] == fr.filename), 0),
                next((f["deletions"] for f in [] if f["filename"] == fr.filename), 0),
                fr.file_summary, json.dumps([i.model_dump() for i in fr.issues])
            ))
        return review_id
```

### `services/reviewer/main.py`

This is the orchestrator. It subscribes to Pub/Sub and runs the full pipeline.

```python
import json
import os
import time
from google.cloud import pubsub_v1
from dotenv import load_dotenv

from pipeline.fetch_diff   import fetch_pr_files
from pipeline.chunk_diff   import chunk_diff
from pipeline.review_chain import review_all_files, generate_pr_summary
from pipeline.aggregate    import build_markdown_comment
from pipeline.post_comment import post_or_update_comment
from utils.cache import make_cache_key, get_cached_review, cache_review
from utils.db   import upsert_repository, save_review

load_dotenv()

def process_pr_event(event: dict) -> None:
    """
    Full pipeline for one PR event.
    Steps: cache check → fetch diff → chunk → review files → summarise → post comment → save to DB
    """
    owner      = event["repo_owner"]
    repo       = event["repo_name"]
    pr_number  = event["pr_number"]
    head_sha   = event["head_sha"]

    print(f"\n▶ Processing PR #{pr_number} in {owner}/{repo} (sha: {head_sha[:7]})")
    start_time = time.time()

    # Step 1: Check Redis cache — skip LLM if we already reviewed this exact commit
    cache_key = make_cache_key(owner, repo, pr_number, head_sha)
    cached = get_cached_review(cache_key)
    if cached:
        print(f"  ↩ Cache hit — skipping LLM calls")
        return

    # Step 2: Fetch changed files + their diffs from GitHub API
    print(f"  → Fetching diff from GitHub...")
    files = fetch_pr_files(owner, repo, pr_number)
    print(f"  → {len(files)} files with diffs")

    if not files:
        print("  ✗ No reviewable files found. Skipping.")
        return

    # Step 3: Chunk/filter the diff
    chunks = chunk_diff(files)
    print(f"  → {len(chunks)} files after filtering")

    # Step 4: Review each file with Gemini
    print(f"  → Running AI review on {len(chunks)} files...")
    file_reviews = review_all_files(chunks)

    # Step 5: Generate PR-level summary
    print(f"  → Generating PR summary...")
    pr_summary = generate_pr_summary(
        file_reviews=file_reviews,
        pr_title=event["pr_title"],
        pr_author=event["pr_author"],
        files_changed=event["changed_files"],
        additions=event["additions"],
        deletions=event["deletions"],
    )

    # Step 6: Build Markdown comment
    processing_ms = int((time.time() - start_time) * 1000)
    markdown_body = build_markdown_comment(pr_summary, file_reviews, pr_number, processing_ms)

    # Step 7: Post or update GitHub comment
    print(f"  → Posting comment to GitHub PR #{pr_number}...")
    comment_id = post_or_update_comment(owner, repo, pr_number, markdown_body)
    print(f"  ✓ Comment posted (id: {comment_id})")

    # Step 8: Save to PostgreSQL
    repo_id = upsert_repository(owner, repo)
    review_id = save_review(repo_id, event, pr_summary, file_reviews, comment_id, processing_ms)
    print(f"  ✓ Saved to DB (review_id: {review_id})")

    # Step 9: Cache the result so we don't re-process the same commit
    cache_review(cache_key, {"review_id": review_id, "comment_id": comment_id})

    print(f"  ✓ Done in {processing_ms}ms")


def pubsub_callback(message) -> None:
    """Called by Pub/Sub client for each incoming message."""
    try:
        event = json.loads(message.data.decode("utf-8"))
        process_pr_event(event)
        message.ack()    # tell Pub/Sub: processed successfully
    except Exception as e:
        print(f"  ✗ Error processing message: {e}")
        message.nack()   # tell Pub/Sub: retry this message


def main():
    project_id       = os.getenv("GCP_PROJECT_ID")
    subscription_id  = os.getenv("PUBSUB_SUBSCRIPTION_ID", "pr-events-sub")

    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = subscriber.subscription_path(project_id, subscription_id)

    print(f"✓ Reviewer service started. Listening on {subscription_path}")

    streaming_pull = subscriber.subscribe(subscription_path, callback=pubsub_callback)

    try:
        streaming_pull.result()   # blocks forever, processing messages as they arrive
    except KeyboardInterrupt:
        streaming_pull.cancel()
        print("Reviewer stopped.")

if __name__ == "__main__":
    main()
```

### `services/reviewer/Dockerfile`
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

---

## 7. Service 3 — REST API (Node.js / Express)

### Purpose
Allow anyone to query the review history stored in PostgreSQL. No authentication required.

### `services/api/package.json`
```json
{
  "name": "pr-review-agent-api",
  "version": "1.0.0",
  "main": "src/index.js",
  "scripts": {
    "start": "node src/index.js",
    "dev": "nodemon src/index.js"
  },
  "dependencies": {
    "express": "^4.19.2",
    "pg": "^8.12.0",
    "dotenv": "^16.4.5",
    "cors": "^2.8.5"
  },
  "devDependencies": {
    "nodemon": "^3.1.4"
  }
}
```

### `services/api/src/db/postgres.js`
```javascript
const { Pool } = require('pg');

const pool = new Pool({ connectionString: process.env.DATABASE_URL });

module.exports = {
  query: (text, params) => pool.query(text, params),
};
```

### `services/api/src/routes/reviews.js`
```javascript
const express = require('express');
const db = require('../db/postgres');
const router = express.Router();

// GET /reviews — list recent reviews (default: last 20)
router.get('/', async (req, res, next) => {
  try {
    const limit = Math.min(parseInt(req.query.limit) || 20, 100);
    const offset = parseInt(req.query.offset) || 0;

    const result = await db.query(`
      SELECT
        r.id, r.pr_number, r.pr_title, r.pr_author, r.pr_url,
        r.risk_level, r.risk_score, r.files_changed,
        r.lines_added, r.lines_removed, r.processing_time_ms, r.created_at,
        repo.owner, repo.repo
      FROM pull_request_reviews r
      JOIN repositories repo ON r.repo_id = repo.id
      ORDER BY r.created_at DESC
      LIMIT $1 OFFSET $2
    `, [limit, offset]);

    res.json({ reviews: result.rows, limit, offset });
  } catch (err) { next(err); }
});

// GET /reviews/:id — full review with file breakdown
router.get('/:id', async (req, res, next) => {
  try {
    const reviewResult = await db.query(`
      SELECT r.*, repo.owner, repo.repo
      FROM pull_request_reviews r
      JOIN repositories repo ON r.repo_id = repo.id
      WHERE r.id = $1
    `, [req.params.id]);

    if (reviewResult.rows.length === 0) {
      return res.status(404).json({ error: 'Review not found' });
    }

    const fileResult = await db.query(
      'SELECT * FROM file_reviews WHERE review_id = $1 ORDER BY id',
      [req.params.id]
    );

    res.json({
      review: reviewResult.rows[0],
      files: fileResult.rows,
    });
  } catch (err) { next(err); }
});

module.exports = router;
```

### `services/api/src/routes/repos.js`
```javascript
const express = require('express');
const db = require('../db/postgres');
const router = express.Router();

// GET /repos/:owner/:repo/stats — aggregate stats for a repo
router.get('/:owner/:repo/stats', async (req, res, next) => {
  try {
    const { owner, repo } = req.params;

    const result = await db.query(`
      SELECT
        COUNT(*)                                    AS total_reviews,
        AVG(r.risk_score)::numeric(5,1)             AS avg_risk_score,
        COUNT(*) FILTER (WHERE r.risk_level='high')   AS high_risk_count,
        COUNT(*) FILTER (WHERE r.risk_level='medium') AS medium_risk_count,
        COUNT(*) FILTER (WHERE r.risk_level='low')    AS low_risk_count,
        AVG(r.processing_time_ms)::integer           AS avg_processing_ms,
        MAX(r.created_at)                            AS last_review_at
      FROM pull_request_reviews r
      JOIN repositories repo ON r.repo_id = repo.id
      WHERE repo.owner = $1 AND repo.repo = $2
    `, [owner, repo]);

    if (result.rows[0].total_reviews === '0') {
      return res.status(404).json({ error: 'No reviews found for this repo' });
    }

    res.json({ repo: `${owner}/${repo}`, stats: result.rows[0] });
  } catch (err) { next(err); }
});

module.exports = router;
```

### `services/api/src/middleware/errorHandler.js`
```javascript
module.exports = (err, req, res, next) => {
  console.error(err.stack);
  res.status(500).json({ error: 'Internal server error', message: err.message });
};
```

### `services/api/src/index.js`
```javascript
require('dotenv').config();
const express = require('express');
const cors = require('cors');
const reviewsRouter = require('./routes/reviews');
const reposRouter = require('./routes/repos');
const errorHandler = require('./middleware/errorHandler');

const app = express();
const PORT = process.env.PORT || 8080;

app.use(cors());
app.use(express.json());

app.get('/health', (req, res) => res.json({ status: 'ok', service: 'api' }));
app.use('/reviews', reviewsRouter);
app.use('/repos', reposRouter);
app.use(errorHandler);

app.listen(PORT, () => console.log(`API running on port ${PORT}`));
```

### `services/api/Dockerfile`
```dockerfile
FROM node:20-slim
WORKDIR /app
COPY package*.json .
RUN npm ci --only=production
COPY src/ src/
EXPOSE 8080
CMD ["node", "src/index.js"]
```

---

## 8. Local Development Setup

### `docker-compose.yml`
```yaml
version: "3.9"
services:

  postgres:
    image: postgres:15
    environment:
      POSTGRES_DB: pr_reviews
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
    ports:
      - "5432:5432"
    volumes:
      - ./database/schema.sql:/docker-entrypoint-initdb.d/schema.sql

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

  webhook:
    build: ./services/webhook
    ports:
      - "8001:8080"
    env_file: .env
    environment:
      - PUBSUB_EMULATOR_HOST=pubsub-emulator:8085
    depends_on: [postgres, redis]

  reviewer:
    build: ./services/reviewer
    env_file: .env
    environment:
      - PUBSUB_EMULATOR_HOST=pubsub-emulator:8085
    depends_on: [postgres, redis]

  api:
    build: ./services/api
    ports:
      - "8002:8080"
    env_file: .env
    depends_on: [postgres]
```

### Local dev steps (run in order)
```bash
# 1. Copy env file and fill in values
cp .env.example .env

# 2. Start local infrastructure
docker-compose up postgres redis -d

# 3. Run schema migrations
psql $DATABASE_URL -f database/schema.sql

# 4. Expose webhook locally using ngrok (for GitHub to reach your laptop)
ngrok http 8001
# Copy the https URL — set it as GitHub webhook URL

# 5. Start services
docker-compose up
```

---

## 9. GCP Deployment

### `infra/pubsub-setup.sh`
```bash
#!/bin/bash
# Run once to create Pub/Sub resources
PROJECT_ID=$1
gcloud pubsub topics create pr-events --project=$PROJECT_ID
gcloud pubsub subscriptions create pr-events-sub \
  --topic=pr-events \
  --ack-deadline=60 \
  --project=$PROJECT_ID
echo "Pub/Sub setup complete"
```

### Deploy to Cloud Run
```bash
# Build and push each service
gcloud builds submit services/webhook --tag gcr.io/$PROJECT_ID/pr-webhook
gcloud builds submit services/reviewer --tag gcr.io/$PROJECT_ID/pr-reviewer
gcloud builds submit services/api --tag gcr.io/$PROJECT_ID/pr-api

# Deploy webhook (public URL, receives GitHub webhooks)
gcloud run deploy pr-webhook \
  --image gcr.io/$PROJECT_ID/pr-webhook \
  --platform managed --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars "GCP_PROJECT_ID=$PROJECT_ID,PUBSUB_TOPIC_ID=pr-events" \
  --set-secrets "GITHUB_WEBHOOK_SECRET=github-webhook-secret:latest,GITHUB_TOKEN=github-token:latest"

# Deploy reviewer (no public URL needed — reads from Pub/Sub)
gcloud run deploy pr-reviewer \
  --image gcr.io/$PROJECT_ID/pr-reviewer \
  --platform managed --region us-central1 \
  --no-allow-unauthenticated \
  --set-env-vars "GCP_PROJECT_ID=$PROJECT_ID,PUBSUB_SUBSCRIPTION_ID=pr-events-sub" \
  --set-secrets "GEMINI_API_KEY=gemini-api-key:latest,GITHUB_TOKEN=github-token:latest,DATABASE_URL=database-url:latest,REDIS_URL=redis-url:latest"

# Deploy API (public URL)
gcloud run deploy pr-api \
  --image gcr.io/$PROJECT_ID/pr-api \
  --platform managed --region us-central1 \
  --allow-unauthenticated \
  --set-secrets "DATABASE_URL=database-url:latest"
```

---

## 10. GitHub Webhook Configuration

1. Go to your GitHub repo → Settings → Webhooks → Add webhook
2. **Payload URL:** `https://your-cloud-run-webhook-url/webhook/github`
3. **Content type:** `application/json`
4. **Secret:** same value as `GITHUB_WEBHOOK_SECRET` in your `.env`
5. **Events:** Select "Let me select individual events" → check **Pull requests** only
6. **Active:** checked

---

## 11. What Each Service Needs from GCP

| Service | GCP APIs to enable |
|---------|-------------------|
| webhook | Cloud Run, Cloud Pub/Sub |
| reviewer | Cloud Run, Cloud Pub/Sub, Secret Manager |
| api | Cloud Run, Secret Manager |
| all | Container Registry (for Docker images) |

Enable with:
```bash
gcloud services enable run.googleapis.com pubsub.googleapis.com \
  secretmanager.googleapis.com containerregistry.googleapis.com
```

---

## 12. API Endpoints Summary

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/reviews` | List recent reviews. Query: `?limit=20&offset=0` |
| GET | `/reviews/:id` | Full review + file breakdown |
| GET | `/repos/:owner/:repo/stats` | Aggregate stats for a repo |

### Example responses

**GET /reviews**
```json
{
  "reviews": [
    {
      "id": 1,
      "pr_number": 47,
      "pr_title": "Add payment retry logic",
      "pr_author": "omtrivedi",
      "risk_level": "medium",
      "risk_score": 42,
      "files_changed": 3,
      "created_at": "2026-06-16T10:30:00Z",
      "owner": "omtrivedioo3",
      "repo": "bitezy"
    }
  ]
}
```

**GET /repos/omtrivedioo3/bitezy/stats**
```json
{
  "repo": "omtrivedioo3/bitezy",
  "stats": {
    "total_reviews": "12",
    "avg_risk_score": "38.5",
    "high_risk_count": "2",
    "medium_risk_count": "7",
    "low_risk_count": "3",
    "avg_processing_ms": 4200,
    "last_review_at": "2026-06-16T10:30:00Z"
  }
}
```

---

## 13. Resume Bullet Points (after you build this)

Add these to your resume under Projects:

> **PR Review Agent** | Python · LangChain · Gemini · GCP Cloud Run · Pub/Sub · Node.js · PostgreSQL · Redis
> - Built an autonomous AI code review agent that processes GitHub PR webhooks via GCP Pub/Sub, uses LangChain + Gemini 1.5 Flash to generate structured per-file reviews, and posts formatted summaries as GitHub PR comments — enabling managers to assess PRs without reading diffs
> - Designed a multi-service event-driven architecture: webhook receiver decoupled from AI reviewer via Pub/Sub, with Redis caching of LLM results (by diff SHA) to eliminate redundant API calls
> - Implemented per-file diff chunking, structured JSON output parsing, and automatic retry logic to handle Gemini rate limits gracefully across 100% free-tier infrastructure

---

*End of specification. All code above is the exact implementation target.*
