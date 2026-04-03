# 🧠 AI-Powered File Organizer for Proton Drive

A comprehensive, production-ready file organization system integrated with the Proton Drive rclone web interface. Features AI-powered content categorization, rule-based organization, duplicate detection, and a modern web dashboard.

## Architecture

```
protondrive-linux/
├── app.py                          # Main Flask app (existing sync manager)
├── ai_organizer/                   # AI organization module
│   ├── __init__.py
│   ├── api.py                      # Flask Blueprint with all AI endpoints
│   ├── database.py                 # PostgreSQL schema, pool, CRUD operations
│   ├── scanner.py                  # File indexer (walks dirs → DB)
│   ├── engines/
│   │   ├── rule_engine.py          # Rule-based organization (extension, MIME, regex, date, creator)
│   │   ├── ai_engine.py            # AI categorization (CLIP local, cloud API placeholder)
│   │   ├── duplicate_engine.py     # Exact (SHA-256) + near-duplicate (perceptual hash)
│   │   └── rclone_engine.py        # Rclone integration with retry & verification
│   ├── utils/
│   │   └── yaml_rules.py           # Import/export rules from YAML
│   └── models/
│       └── __init__.py             # Placeholder for future AI models
├── templates/ai/                   # Web UI templates
│   ├── base_ai.html                # Layout with sidebar navigation
│   ├── dashboard.html              # Stats, charts, quick actions
│   ├── organize.html               # Scan → Propose → Apply workflow
│   ├── duplicates.html             # Duplicate detection & resolution
│   ├── rules.html                  # CRUD for organization rules
│   ├── ai_settings.html            # AI provider & category config
│   ├── db_settings.html            # PostgreSQL connection config
│   ├── rclone_settings.html        # Rclone transfer settings
│   └── extensions.html             # Future module placeholders
├── static/css/ai.css               # AI module styles
├── static/js/ai.js                 # Shared JS utilities
└── config/organize-rules.yml       # Default rule definitions
```

## Features

### 📊 Dashboard
- File count, total size, pending duplicates, AI analyses, active rules
- File type breakdown chart
- Recent job history with status tracking
- Quick-action buttons for scan, organize, detect, analyze

### 📂 File Organization
- **Rule-based**: Extension, MIME type, regex, date-based (YYYY/MM), creator-based
- **AI-powered**: Zero-shot image classification using CLIP
- **Workflow**: Scan → Generate Proposals → Review → Apply (with dry-run support)
- **YAML import/export**: Compatible with organize-rules.yml format

### 🔍 Duplicate Detection
- **Exact**: SHA-256 hash matching
- **Near-duplicate**: Perceptual hashing (dHash) for visually similar images
- **Resolution UI**: Review groups, mark keep/delete per member

### 🤖 AI Integration
- **Local**: OpenAI CLIP (clip-vit-base-patch32) for zero-shot categorization
- **Cloud**: Placeholder provider for OpenAI Vision, Google Cloud Vision, etc.
- **Extensible**: Add providers by subclassing `BaseAIProvider`
- **Configurable**: Custom categories, batch limits, auto-analyze toggle

### 🗄️ Database
- PostgreSQL with connection pooling (psycopg2)
- Full schema: files, AI analysis, duplicate groups, rules, preferences, jobs, tags, collections
- Web UI for connection testing and schema initialization

### 🔄 Rclone Integration
- Transfer settings (checkers, transfers, bandwidth limit)
- Retry logic with configurable max retries and delay
- Post-transfer verification via `rclone check`
- Remote status and storage usage display

### 🧩 Extension Points (Planned)
- 👤 Facial Recognition — face detection and clustering
- 🏷️ Auto-Tagging — automatic keyword tagging
- 📚 Smart Collections — auto-updating collections
- 🔎 Semantic Search — search by content or visual similarity
- 📸 Google Photos / ☁️ Google Drive / 📦 Dropbox integration

## Setup

### Prerequisites
- Python 3.10+
- PostgreSQL 14+
- rclone (for sync features)

### Installation

```bash
# Install Python dependencies
pip install -r requirements.txt

# For local AI features (optional, ~600MB download):
pip install torch transformers

# Start PostgreSQL and create database
sudo service postgresql start
sudo -u postgres psql -c "CREATE USER protondrive WITH PASSWORD 'protondrive';"
sudo -u postgres psql -c "CREATE DATABASE protondrive_ai OWNER protondrive;"

# Run the app
python app.py --port 5000
```

### First-time Setup
1. Open `http://localhost:5000/ai/dashboard`
2. Go to **Database** → Test connection → **Initialize Schema**
3. Go to **Rules** → **Import YAML** → enter path to `config/organize-rules.yml`
4. Go to **Dashboard** → **Scan Directory** → enter your sync folder path
5. Review proposals in **Organize Files**

## API Reference

All AI endpoints are under `/ai/api/`:

| Endpoint | Method | Description |
|---|---|---|
| `/ai/api/stats` | GET | Dashboard statistics |
| `/ai/api/scan` | POST | Start directory scan |
| `/ai/api/organize/propose` | POST | Generate organization proposals |
| `/ai/api/organize/apply` | POST | Apply proposals (dry-run or live) |
| `/ai/api/rules` | GET/POST | List / create rules |
| `/ai/api/rules/<id>` | PUT/DELETE | Update / delete rule |
| `/ai/api/rules/import` | POST | Import rules from YAML |
| `/ai/api/rules/export` | POST | Export rules to YAML |
| `/ai/api/duplicates/detect` | POST | Start duplicate detection |
| `/ai/api/duplicates/groups` | GET | List duplicate groups |
| `/ai/api/duplicates/groups/<id>` | GET | Group detail with members |
| `/ai/api/duplicates/resolve` | POST | Mark keep/delete on members |
| `/ai/api/ai/analyze` | POST | Start batch AI analysis |
| `/ai/api/ai/providers` | GET | List available AI providers |
| `/ai/api/ai/settings` | GET/PUT | AI settings |
| `/ai/api/db/test` | POST | Test DB connection |
| `/ai/api/db/settings` | GET/PUT | DB connection settings |
| `/ai/api/db/init` | POST | Initialize schema |
| `/ai/api/rclone/status` | GET | Rclone version check |
| `/ai/api/rclone/settings` | GET/PUT | Rclone transfer settings |
| `/ai/api/files` | GET | List indexed files |
| `/ai/api/jobs` | GET | Recent job history |
| `/ai/api/job/<id>` | GET | Job status |
| `/ai/api/extensions` | GET | List extension modules |

## Adding a Custom AI Provider

```python
from ai_organizer.engines.ai_engine import BaseAIProvider, PROVIDERS

class MyCloudProvider(BaseAIProvider):
    name = "my_cloud"

    def categorize_image(self, filepath: str) -> dict:
        # Your API call here
        return {"top_category": "...", "confidence": 0.95}

PROVIDERS["my_cloud"] = MyCloudProvider
```

## License

See LICENSE file in the project root.
