# Dynamic Schema

A production-grade platform for managing dynamic form schemas. Built on Django 5.0+ with PostgreSQL, it provides a flexible, version-controlled schema engine that allows non-technical users to define complex form structures through a hierarchical node system. Suitable for enterprise applications, government services, healthcare, finance, education, and any domain requiring dynamic data collection.

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://python.org)
[![Django](https://img.shields.io/badge/Django-5.0+-green.svg)](https://djangoproject.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15+-orange.svg)](https://postgresql.org)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [Installation](#installation)
- [Usage](#usage)
- [API Documentation](#api-documentation)
- [Configuration](#configuration)
- [Development](#development)
- [Deployment](#deployment)
- [Database Migrations](#database-migrations)
- [Contributing](#contributing)
- [License](#license)

---

## Overview

Dynamic Schema solves the challenge of rapidly evolving form requirements in enterprise environments. Instead of hardcoding form structures, this platform enables:

- **Non-technical users** to design and modify forms through an intuitive admin interface
- **Version control** for every schema change (draft → published → archived lifecycle)
- **Hierarchical composition** of complex forms from reusable building blocks
- **Type-safe attributes** with strong data type definitions and domain constraints
- **Multi-tenant support** for organizations and projects
- **Industry agnostic** - used in government, healthcare, finance, education, and more

### Core Purpose

- Enable dynamic form and data structure creation without code changes
- Maintain strict version control of schemas with full audit trail
- Provide a hierarchical node-based system for complex data modeling
- Ensure enterprise-grade data integrity through PostgreSQL schema isolation (`s7` schema)

---

## Key Features

### Schema Versioning
- Every form has a unique `key` + `version` combination
- Strict mutational control (immutable published schemas)
- Draft → Published → Archived lifecycle
- Build counter tracking for cache invalidation

### Hierarchical Composition
- Forms built from nodes that can contain child nodes
- `NodeTypeComposition` defines parent-child relationships
- Support for unlimited nesting depth
- Self-referencing hierarchies (e.g., taxonomy categories with subcategories)

### Type-Safe Attributes
- Strong typing via `DataType` definitions
- Optional `Domain` constraints for enumerated values
- Variant-specific attributes (e.g., different properties for text vs. number fields)
- Universal attributes that apply to all variants

### Cache Layer
- Pre-computed JSON schemas cached in `FormSchemaCache`
- Automatic cache invalidation on schema changes
- Optimized for high-read scenarios

### PostgreSQL s7 Integration
- Business-critical logic encapsulated in PL/pgSQL functions
- Atomic schema import/export operations
- Database-level referential integrity

---

## Architecture

### Four-Layer Architecture

The project follows **Clean Architecture** principles with clear separation of concerns, making it maintainable and testable for teams of any size:

```
┌─────────────────────────────────────────────────────────────┐
│                     Views/API Endpoints                     │
│  (HTTP request/response, serialization, authentication)     │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                        Services                             │
│  (Business logic, orchestration, transaction management)    │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                      Repositories                           │
│  (Data access, SQL execution, s7 function invocation)       │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                        Models                               │
│  (Django ORM definitions, database schema, constraints)     │
└─────────────────────────────────────────────────────────────┘
```

### Layer Responsibilities

| Layer | Responsibility | Forbidden Operations |
|-------|----------------|---------------------|
| **Models** | Database schema, constraints, indexes | Business logic, direct queries |
| **Repositories** | Data access, s7 function calls | Business rules, HTTP concerns |
| **Services** | Orchestration, business rules, transactions | Direct DB access, view concerns |
| **Views** | HTTP handling, serialization, auth | Repository access, business logic |

---

## Quick Start

### Using Docker (Recommended)

```bash
# Clone the repository
git clone git@github.com:chiunti/dynamic_schema_s7.git
cd dynamic_schema_s7

# Copy environment variables
cp .env.example .env

# Build and run with Docker Compose
docker compose up --build

# Run migrations
docker compose exec web python manage.py migrate

# Create superuser
docker compose exec web python manage.py createsuperuser

# Access the application
# - Admin: http://localhost:8000/admin/
# - API: http://localhost:8000/api/
```

### Local Development

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure database in .env
cp .env.example .env
# Edit .env with your PostgreSQL credentials
# Note: Use DJANGO_SECRET_KEY, DJANGO_DEBUG, DJANGO_ALLOWED_HOSTS for Django settings
# Use POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD, etc. for database settings

# Run migrations
python manage.py migrate

# Create superuser
python manage.py createsuperuser

# Run development server
python manage.py runserver
```

---

## Installation

### Prerequisites

- Python 3.12 or higher
- PostgreSQL 15 or higher

### Step-by-Step Installation

1. **Install Python dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Create PostgreSQL database**
   ```sql
   CREATE DATABASE dynamic_schema;
   CREATE USER dynamic_schema WITH PASSWORD 'your_password';
   GRANT ALL PRIVILEGES ON DATABASE dynamic_schema TO dynamic_schema;
   ```

3. **Configure environment variables**
   ```bash
   cp .env.example .env
   # Edit .env with your database credentials
   ```

4. **Run migrations**
   ```bash
   python manage.py migrate
   ```

5. **Create admin user**
   ```bash
   python manage.py createsuperuser
   ```

---

## Usage

### Creating a Form Schema

1. **Access the Admin Interface**
   - Navigate to `http://localhost:8000/admin/`
   - Log in with your superuser credentials

2. **Create Node Types**
   - Go to `Schemas > Node types`
   - Define the structure of your form elements

3. **Define Compositions**
   - Go to `Schemas > Node type compositions`
   - Set up parent-child relationships between node types

4. **Create Attribute Definitions**
   - Go to `Schemas > Attribute definitions`
   - Define the properties each node type can have

5. **Build Your Form**
   - Go to `Schemas > Nodes`
   - Create the actual form structure using the visual editor

### API Endpoints

The project provides admin-facing API endpoints for managing schemas:

```bash
# Component types (for admin UI)
GET /admin/schemas/api/component-types/

# Component properties by type
GET /admin/schemas/api/component-properties/<component_type>/
POST /admin/schemas/api/component-properties/<component_type>/save/

# Variants by scope
GET /admin/schemas/api/variants/

# Attributes by variant
GET /admin/schemas/api/attributes-by-variant/<variant_key>/

# Attribute definitions management
POST /admin/schemas/api/create-attribute-def/
POST /admin/schemas/api/update-attribute-common/
POST /admin/schemas/api/update-attribute-specific/
POST /admin/schemas/api/delete-attribute-def/
```

For public schema consumption, use the endpoints in `schemas/api_views.py`.

---

## API Documentation

The API follows RESTful principles with JSON payloads. Authentication is required for write operations.

### Authentication

The API uses Django session authentication. Login via the admin interface (`/admin/`) or implement your own authentication views.

All write operations (POST, PATCH, PUT, DELETE) require authentication. The schema endpoint (`GET /api/schema/...`) is publicly readable.

### Core Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/schema/{node_type}/{key}/{version}/` | Get published schema by type, key, and version (public, cached) |

**Schema Response Format:**
```json
{
  "data": { /* schema JSON */ },
  "meta": {
    "node_type": "string",
    "key": "string",
    "version": "string"
  }
}
```

### Organizations

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/organizations/` | List organizations |
| POST | `/api/organizations/` | Create organization |
| GET | `/api/organizations/{id}/` | Get organization details |
| PATCH/PUT | `/api/organizations/{id}/` | Update organization |
| GET | `/api/organizations/{id}/members/` | List members |
| POST | `/api/organizations/{id}/members/` | Add member |
| DELETE | `/api/organizations/{id}/members/{member_id}/` | Remove member |

### Projects

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/projects/` | List projects |
| POST | `/api/projects/` | Create project |
| GET | `/api/projects/{id}/` | Get project details |
| PATCH/PUT | `/api/projects/{id}/` | Update project |
| DELETE | `/api/projects/{id}/` | Delete project |

All endpoints require authentication except `/api/schema/{...}/`.

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DJANGO_DEBUG` | `1` | Enable debug mode (0/1, true/false) |
| `DJANGO_SECRET_KEY` | Required | Django secret key |
| `POSTGRES_DB` | `dynamic_schema` | PostgreSQL database name |
| `POSTGRES_USER` | `dynamic_schema` | PostgreSQL username |
| `POSTGRES_PASSWORD` | Required | PostgreSQL password |
| `POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `POSTGRES_SCHEMA` | `s7,public` | PostgreSQL search path |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated allowed hosts |
| `TIME_ZONE` | `America/Mexico_City` | Application timezone |
| `LANGUAGE_CODE` | `en-us` | Language code |

### Database Configuration

The system uses PostgreSQL with an isolated `s7` schema for business logic:

```python
# config/settings/base.py
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get('POSTGRES_DB', ''),
        "USER": os.environ.get('POSTGRES_USER', ''),
        "PASSWORD": os.environ.get('POSTGRES_PASSWORD', ''),
        "HOST": os.environ.get('POSTGRES_HOST', 'localhost'),
        "PORT": os.environ.get('POSTGRES_PORT', '5432'),
        "OPTIONS": {
            "options": f"-c search_path={os.environ.get('POSTGRES_SCHEMA', 'public')}"
        },
    }
}
```

---

## Development

### Running Tests

The project includes a placeholder for tests at `schemas/tests.py`. Add your test cases following Django's testing framework:

```bash
# Run all tests
python manage.py test

# Run tests for specific app
python manage.py test schemas
```

See [Django Testing Documentation](https://docs.djangoproject.com/en/5.0/topics/testing/) for writing tests.

### Database Migrations

```bash
# Create new migration
python manage.py makemigrations

# Apply migrations
python manage.py migrate

# Rollback migration
python manage.py migrate schemas 0004
```

### Admin UI Development

The admin interface uses custom JavaScript for the visual node editor:

```bash
# Navigate to admin static files
cd schemas/static/admin/js/

# The main editor is in editor.js
# Templates are in schemas/templates/admin/
```

---

## Deployment

### Docker Deployment

```bash
# Build and run with Docker Compose
docker compose up --build -d

# Run migrations
docker compose exec web python manage.py migrate

# Create superuser
docker compose exec web python manage.py createsuperuser
```

### Manual Deployment

1. **Set environment variables**
   ```bash
   export DJANGO_DEBUG=0
   export DJANGO_SECRET_KEY=your-production-secret
   export DJANGO_ALLOWED_HOSTS=yourdomain.com
   export POSTGRES_DB=dynamic_schema
   export POSTGRES_USER=dynamic_schema
   export POSTGRES_PASSWORD=your-database-password
   export POSTGRES_HOST=your-db-host
   export POSTGRES_PORT=5432
   ```

2. **Collect static files**
   ```bash
   python manage.py collectstatic --no-input
   ```

3. **Run migrations**
   ```bash
   python manage.py migrate
   ```

4. **Start with Gunicorn**
   ```bash
   gunicorn config.wsgi:application -b 0.0.0.0:8000
   ```

---

## Project Structure

```
dynamic_schema_s7/
├── accounts/               # User authentication
├── config/                 # Django configuration
│   ├── settings/           # Environment-specific settings
│   ├── urls.py             # URL routing
│   ├── asgi.py             # ASGI entry point
│   └── wsgi.py             # WSGI entry point
├── schemas/                # Core application
│   ├── admin/              # Admin interface customizations
│   ├── constants/          # Constants and enums
│   ├── management/         # Django management commands
│   ├── migrations/         # Database migrations
│   ├── repositories/       # Data access layer
│   ├── services/           # Business logic
│   ├── static/             # Static files (admin UI)
│   ├── templates/          # Admin templates
│   ├── admin_api_views.py  # Admin-facing API endpoints
│   ├── api_views.py        # Public API endpoints
│   ├── constants.py        # Application constants
│   ├── models.py           # Data models
│   ├── urls.py             # Schema app URL routing
│   ├── utils.py            # Utility functions
│   ├── views.py            # Schema views
│   └── tests.py            # Test cases
├── .env.example            # Environment variables template
├── Dockerfile              # Docker configuration
├── docker-compose.yml      # Development Docker Compose
├── docker-compose.prod.yml # Production Docker Compose
├── requirements.txt        # Python dependencies
├── manage.py               # Django management
└── README.md               # This file
```

---

## Example Use Cases

### Customer Satisfaction Survey (Included Example)

The project includes a complete **Survey System** example in `0005_example_seed.py` demonstrating how to model a real-world data-collection use case end-to-end.

#### What Gets Created

| Layer | Items |
|-------|-------|
| **Domains** | `SurveyStatus` (draft/published/closed), `QuestionType` (text, email, number, single_choice, multiple_choice, rating, textarea, date, boolean), `SatisfactionLevel` (1-5 scale), `ServiceUsed` (support, delivery, website, mobile_app, in_store), `ScoreRange` (min/max for int_tuple) |
| **NodeTypes** | `survey` (root, container), `survey_section` (container), `survey_question` (leaf) |
| **Compositions** | survey → survey_section, survey_section → survey_question |
| **AttributeDefs** | `survey`: id, title, description, status, anonymous, start/end date, response_count, plus showcase attributes (brand_color, tags, allowed_scores, score_range, meta). `survey_section`: id, title, description, sort_order. `survey_question` universal: id, label, type, required, placeholder, help_text, sort_order |
| **Variants** | 9 variants for `survey_question`: text, email, number, single_choice, multiple_choice, rating, textarea, date, boolean — each with type-specific properties (e.g., `options` for choice, `min/max_value` for number/rating, `min/max_lines` for textarea) |
| **Organization** | "Demo Corp" (`demo-corp`) |
| **Project** | "Customer Experience" (`customer-experience`) |
| **Survey Instance** | "Customer Satisfaction Survey 2026" (key: customer-satisfaction-2026, title: Customer Satisfaction Survey 2026) with 2 sections and 8 questions |

#### Hierarchy Visualization

```
Survey: Customer Satisfaction Survey 2026  (key: customer-satisfaction-2026)
├── Section 1: About You
│   ├── Question: What is your name?          (text, required)
│   ├── Question: Email address               (email, required)
│   └── Question: Your age                    (number, min:18, max:100)
└── Section 2: Your Experience
    ├── Question: Which services have you used?   (multiple_choice)
    ├── Question: How satisfied are you?          (rating, 1-5)
    ├── Question: Would you recommend us?         (boolean)
    ├── Question: When did you last visit?        (date)
    └── Question: Any additional comments?        (textarea)
```

#### Running the Example

```bash
# Apply all migrations including the survey seed
python manage.py migrate

# Verify the survey was created
python manage.py shell -c "
from schemas.models import Node, NodeType, NodeAttribute, AttributeDef
survey = Node.objects.get(key='customer-satisfaction-2026')
title_attr = NodeAttribute.objects.filter(node=survey, attribute_def__json_key='title').first()
print(f'Survey name: {survey.name}')
print(f'Survey title: {title_attr.value_string if title_attr else \"N/A\"}')
print(f'Sections: {survey.node_set.count()}')
print(f'Total questions: {Node.objects.filter(node_type__name=\"survey_question\").count()}')
"

# Reverse the example (removes all survey data)
python manage.py migrate schemas 0004
```

#### Extending the Example

To create your own schema structure, follow the same pattern:

1. **Define Domains** — Enumerated value sets for constrained fields
2. **Define NodeTypes** — The building blocks (root, containers, leaves)
3. **Define Compositions** — Parent-child relationships between NodeTypes
4. **Define AttributeDefs** — Properties each NodeType can have (with DataType + optional Domain)
5. **Create Nodes** — Instances of NodeTypes arranged in a hierarchy
6. **Set NodeAttributes** — Actual values for each node's attributes

---

## Database Migrations

The migration sequence is designed for clean first-time deployment:

| Migration | Description |
|-----------|-------------|
| `0001_s7_structure.py` | Core models, constraints, indexes |
| `0002_s7_routines.py` | PostgreSQL functions, triggers |
| `0003_s7_views.py` | Database views |
| `0004_base_seed.py` | Base seed data (universal DataTypes) |
| `0005_example_seed.py` | Survey System example (organization, project, full survey) |

The public release includes migrations 0001-0005. Additional domain-specific migrations (forms, SDUI, etc.) can be created privately.

---

## Contributing

We welcome contributions! Please follow these guidelines:

1. **Fork the repository**
2. **Create a feature branch**: `git checkout -b feature/my-feature`
3. **Follow the coding standards** (UUID primary keys, SQL parameter binding, transactions)
4. **Write tests** for new functionality
5. **Submit a pull request** with clear description

### Coding Standards

- **UUID Primary Keys**: Always use `gen_random_uuid()` for primary keys
- **Table Naming**: Use `schema_*` prefix for all tables
- **SQL Safety**: Never use string interpolation in SQL queries
- **Transactions**: Use `@transaction.atomic` for state-changing operations
- **Type Hints**: Use complete type hints for all functions
- **s7 Functions**: Use parameter binding with `connection.cursor()`

See code comments and docstrings for detailed conventions.

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## Support

- **Issues**: Report issues via your project's issue tracker
- **Documentation**: See this README and AGENTS.md for detailed development guidelines
- **Health Check**: `/health/` endpoint verifies database connectivity

---

## Acknowledgments

- Battle-tested in production environments with strict compliance requirements
- Designed for security, auditability, and regulatory compliance
- Scales from small teams to enterprise deployments

---

**Made with ❤️ for better software**
