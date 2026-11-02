# ERD: Microfinance IS

Схема совпадает с ERD команды. Поля, которые мы **добавили** для регистрации и входа, помечены в комментариях
(`added`): `clients.email`, `clients.password_hash`, `clients.marital_status`, `clients.created_at`,
`employees.password_hash`, а также `loan_applications.term_months`, `purpose`, `approval_type`, `created_at`.

```mermaid
erDiagram
    CLIENTS ||--o{ EMPLOYMENT : has
    CLIENTS ||--o{ DOCUMENTS : uploads
    CLIENTS ||--o{ LOAN_APPLICATIONS : submits
    EMPLOYEES |o--o{ LOAN_APPLICATIONS : reviews
    LOAN_APPLICATIONS ||--o{ CLIENT_CHECKS : gets
    LOAN_APPLICATIONS ||--o| LOANS : becomes
    LOANS ||--o{ REPAYMENTS : has

    CLIENTS {
        int client_id PK
        string iin UK
        string full_name
        date birth_date
        string phone
        string address
        string email UK "added"
        string password_hash "added"
        string marital_status "added"
        datetime created_at "added"
    }
    EMPLOYEES {
        int employee_id PK
        string full_name
        string role
        string login UK
        string password_hash "added"
    }
    EMPLOYMENT {
        int employment_id PK
        int client_id FK
        string employer_name
        string position
        decimal monthly_income
    }
    DOCUMENTS {
        int document_id PK
        int client_id FK
        string doc_type
        string file_path
        string file_hash "sha256"
    }
    LOAN_APPLICATIONS {
        int application_id PK
        int client_id FK
        int employee_id FK "nullable"
        decimal requested_amount
        int term_months "added"
        string purpose "added"
        string status
        string approval_type "added: Auto/Manual"
        datetime created_at "added"
    }
    CLIENT_CHECKS {
        int check_id PK
        int application_id FK
        string check_type
        string result_status
        datetime checked_at
    }
    LOANS {
        int loan_id PK
        int application_id FK,UK "1-to-1"
        decimal approved_amount
        decimal interest_rate
        date issue_date
    }
    REPAYMENTS {
        int payment_id PK
        int loan_id FK
        date due_date
        decimal amount_due
        string status
    }
```

## Связи и правила удаления

| Связь | Тип | FOREIGN KEY | При удалении родителя |
|-------|-----|-------------|------------------------|
| clients → employment (has) | 1 : N | `employment.client_id → clients.client_id` | CASCADE |
| clients → documents (uploads) | 1 : N | `documents.client_id → clients.client_id` | CASCADE |
| clients → loan_applications (submits) | 1 : N | `loan_applications.client_id → clients.client_id` | CASCADE |
| employees → loan_applications (reviews) | 0..1 : N | `loan_applications.employee_id → employees.employee_id` (может быть NULL) | SET NULL |
| loan_applications → client_checks (gets) | 1 : N | `client_checks.application_id → loan_applications.application_id` | CASCADE |
| loan_applications → loans (becomes) | 1 : 0..1 | `loans.application_id` (UNIQUE) → `loan_applications.application_id` | CASCADE |
| loans → repayments (has) | 1 : N | `repayments.loan_id → loans.loan_id` | CASCADE |

Внешние ключи в SQLite работают только после `PRAGMA foreign_keys = ON`. Это включается в `database.get_connection()` для каждого соединения.

Типы в SQLite: `decimal` хранится как `REAL`, `date`/`datetime` хранятся как `TEXT` в формате ISO (`2026-11-02`, `2026-11-02 15:00:00`).
