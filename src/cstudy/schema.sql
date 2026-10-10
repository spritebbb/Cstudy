-- C语言备考助手 数据库结构
-- 由 scripts/init_db.py 执行。可以重复运行，不会丢数据。

PRAGMA foreign_keys = ON;

-- ============ 考纲知识点 ============
CREATE TABLE IF NOT EXISTS syllabus_points (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,          -- 知识点名称
    chapter    TEXT,                             -- 所属章节
    status     TEXT    NOT NULL DEFAULT '未学习', -- 未学习 / 学习中 / 已掌握
    sort_order INTEGER NOT NULL DEFAULT 0        -- 展示顺序
);

-- ============ 视频 ============
CREATE TABLE IF NOT EXISTS videos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bvid       TEXT    NOT NULL,                 -- BV 号
    page       INTEGER NOT NULL DEFAULT 1,       -- 第几个分 P
    title      TEXT,
    url        TEXT,
    duration   INTEGER,                          -- 秒
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (bvid, page)
);

-- ============ 字幕（缓存，避免重复抓取）============
CREATE TABLE IF NOT EXISTS subtitles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id   INTEGER NOT NULL,
    source     TEXT    NOT NULL,                 -- bilibili_cc / whisper
    content    TEXT    NOT NULL,                 -- 带时间戳的可读文本，方便直接查看
    segments   TEXT,                             -- JSON，程序用（保留时间信息）
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
);

-- ============ 学习笔记 ============
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id   INTEGER,
    title      TEXT,
    content    TEXT    NOT NULL,                 -- Markdown 正文
    file_path  TEXT,                             -- notes/ 下的文件路径
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE SET NULL
);

-- ============ 笔记 <-> 知识点（多对多）============
CREATE TABLE IF NOT EXISTS note_points (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id  INTEGER NOT NULL,
    point_id INTEGER NOT NULL,
    UNIQUE (note_id, point_id),
    FOREIGN KEY (note_id)  REFERENCES notes(id)            ON DELETE CASCADE,
    FOREIGN KEY (point_id) REFERENCES syllabus_points(id)  ON DELETE CASCADE
);

-- ============ 题目 ============
CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL CHECK (type IN ('choice', 'fill', 'reading', 'coding')),
    difficulty  TEXT CHECK (difficulty IN ('easy', 'medium', 'hard')),
    point       TEXT,                            -- 知识点名称（AI 给的文字标签）
    point_id    INTEGER,                         -- 对应考纲知识点（暂时没用上，恒为 NULL）
    note_id     INTEGER,                         -- 来自哪份笔记
    question    TEXT NOT NULL,
    options     TEXT,                            -- 选择题选项，JSON 数组
    answer      TEXT NOT NULL,
    explanation TEXT,
    source      TEXT,                            -- 来源标记，如 video:BV1xx#p3
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (point_id) REFERENCES syllabus_points(id) ON DELETE SET NULL,
    FOREIGN KEY (note_id)  REFERENCES notes(id)           ON DELETE SET NULL
);

-- ============ 答题记录 ============
CREATE TABLE IF NOT EXISTS answers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL,
    user_answer TEXT,
    is_correct  BOOLEAN NOT NULL DEFAULT 0,
    answered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    time_spent  INTEGER,                         -- 秒
    FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE
);

-- ============ 错题本 ============
CREATE TABLE IF NOT EXISTS wrong_questions (
    question_id    INTEGER PRIMARY KEY,
    wrong_count    INTEGER NOT NULL DEFAULT 0,
    last_wrong_at  TIMESTAMP,
    next_review_at TIMESTAMP,                    -- 遗忘曲线下次复习时间
    mastered       BOOLEAN NOT NULL DEFAULT 0,
    FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE
);

-- ============ 索引 ============
CREATE INDEX IF NOT EXISTS idx_subtitles_video    ON subtitles(video_id);
CREATE INDEX IF NOT EXISTS idx_notes_video        ON notes(video_id);
CREATE INDEX IF NOT EXISTS idx_questions_note     ON questions(note_id);
CREATE INDEX IF NOT EXISTS idx_questions_point    ON questions(point_id);
CREATE INDEX IF NOT EXISTS idx_answers_question   ON answers(question_id);
CREATE INDEX IF NOT EXISTS idx_wrong_next_review  ON wrong_questions(next_review_at);
