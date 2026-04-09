-- Key Portal Usage Database Schema

-- Table 1: Daily total usage (保留每日总体统计)
CREATE TABLE IF NOT EXISTS daily_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,  -- YYYY-MM-DD
    total_requests INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_daily_usage_date ON daily_usage(date);


-- Table 2: User usage by date (按用户+日期的详细统计)
CREATE TABLE IF NOT EXISTS user_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,  -- YYYY-MM-DD
    user_email TEXT NOT NULL,
    api_key TEXT NOT NULL,
    total_requests INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(date, user_email, api_key)
);

CREATE INDEX IF NOT EXISTS idx_user_usage_date ON user_usage(date);
CREATE INDEX IF NOT EXISTS idx_user_usage_email ON user_usage(user_email);
CREATE INDEX IF NOT EXISTS idx_user_usage_date_email ON user_usage(date, user_email);


-- Table 3: Request details (可选：存储详细的请求记录，用于审计)
CREATE TABLE IF NOT EXISTS request_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    api_key TEXT NOT NULL,
    user_email TEXT,
    model TEXT,
    auth_index TEXT,
    source TEXT,
    failed INTEGER DEFAULT 0,  -- 0=success, 1=failed
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_request_logs_timestamp ON request_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_request_logs_api_key ON request_logs(api_key);
CREATE INDEX IF NOT EXISTS idx_request_logs_user_email ON request_logs(user_email);
