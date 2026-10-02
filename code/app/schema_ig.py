"""Instagram bo'limi jadvallari (ig_*). Boshqa bo'limlardan butunlay alohida."""
T = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"

TABLES = [
    f"""CREATE TABLE IF NOT EXISTS ig_accounts(
        id INT AUTO_INCREMENT PRIMARY KEY, ig_user_id VARCHAR(40) NOT NULL, username VARCHAR(120), name VARCHAR(255),
        account_type VARCHAR(30), bio TEXT, website VARCHAR(600), followers INT DEFAULT 0, follows INT DEFAULT 0,
        media_count INT DEFAULT 0, picture TEXT, token TEXT, token_expires VARCHAR(32), token_at VARCHAR(32),
        status VARCHAR(16) DEFAULT 'active', last_error TEXT, goal_text TEXT, goal_json LONGTEXT, goal_target INT DEFAULT 0,
        goal_date VARCHAR(10), lang VARCHAR(16) DEFAULT 'uz', niche TEXT, audience TEXT, tone TEXT, offer TEXT,
        extra TEXT, synced_at VARCHAR(32), created_at VARCHAR(32), demo_at VARCHAR(32),
        UNIQUE KEY u_igacc(ig_user_id)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ig_daily(
        account_id INT NOT NULL, day VARCHAR(10) NOT NULL, followers INT, follows INT, media_count INT,
        reach INT, views INT, interactions INT, profile_views INT, likes INT, comments INT, shares INT, saves INT,
        new_followers INT, PRIMARY KEY(account_id, day)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ig_media(
        id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL, ig_id VARCHAR(40) NOT NULL, permalink VARCHAR(600),
        caption LONGTEXT, mtype VARCHAR(24), product VARCHAR(16), ts VARCHAR(32), likes INT DEFAULT 0, comments INT DEFAULT 0,
        reach INT DEFAULT 0, views INT DEFAULT 0, saved INT DEFAULT 0, shares INT DEFAULT 0, interactions INT DEFAULT 0,
        watch_ms INT DEFAULT 0, thumb TEXT, fetched_at VARCHAR(32), insights_at VARCHAR(32), plan_id INT,
        UNIQUE KEY u_igmedia(account_id, ig_id), INDEX idx_igmedia(account_id, ts)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ig_demo(
        account_id INT NOT NULL, kind VARCHAR(24) NOT NULL, data LONGTEXT, updated_at VARCHAR(32),
        PRIMARY KEY(account_id, kind)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ig_plan(
        id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL, title VARCHAR(255), caption LONGTEXT, media_json LONGTEXT,
        mtype VARCHAR(16) DEFAULT 'IMAGE', scheduled_at VARCHAR(32), status VARCHAR(16) DEFAULT 'draft', mode VARCHAR(10),
        ig_media_id VARCHAR(40), permalink VARCHAR(600), sent_at VARCHAR(32), error TEXT, idea_id INT, created_at VARCHAR(32),
        reminded INT DEFAULT 0, notes TEXT, INDEX idx_igplan(account_id, status, scheduled_at)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ig_ideas(
        id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL, created_at VARCHAR(32), kind VARCHAR(16) DEFAULT 'idea',
        lang VARCHAR(8), fmt VARCHAR(16), note TEXT, title VARCHAR(400), body_json LONGTEXT, status VARCHAR(16) DEFAULT 'new',
        model VARCHAR(80), cost DOUBLE DEFAULT 0, plan_id INT, INDEX idx_igideas(account_id, created_at)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ig_reports(
        id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL, kind VARCHAR(16), created_at VARCHAR(32), body_json LONGTEXT,
        model VARCHAR(80), cost DOUBLE DEFAULT 0, INDEX idx_igrep(account_id, kind, created_at)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS ig_log(
        id INT AUTO_INCREMENT PRIMARY KEY, account_id INT, ts VARCHAR(32), kind VARCHAR(20), info TEXT,
        INDEX idx_iglog(account_id, ts)) {T}""",
    f"""CREATE TABLE IF NOT EXISTS cf_files(
        id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(255) NOT NULL, `key` VARCHAR(300) NOT NULL, size BIGINT DEFAULT 0,
        ctype VARCHAR(80), kind VARCHAR(10), created_at VARCHAR(32), used_n INT DEFAULT 0, last_used_at VARCHAR(32),
        UNIQUE KEY u_cfkey(`key`), INDEX idx_cfname(name)) {T}""",
]

TABLE_NAMES = ["ig_accounts", "ig_daily", "ig_media", "ig_demo", "ig_plan", "ig_ideas", "ig_reports", "ig_log", "cf_files"]
