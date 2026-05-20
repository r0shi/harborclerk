--
-- PostgreSQL database dump
--

\restrict HibCzWg7RyoRD2yGl5tJuWjhPYNL0TqVY6wWBF0dKceynyy3ag8O8MRcNodlkUQ

-- Dumped from database version 18.3
-- Dumped by pg_dump version 18.3

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: lka
--

-- *not* creating schema, since initdb creates it


ALTER SCHEMA public OWNER TO lka;

--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: lka
--

COMMENT ON SCHEMA public IS '';


--
-- Name: citext; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public;


--
-- Name: EXTENSION citext; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION citext IS 'data type for case-insensitive character strings';


--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: 
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: job_stage; Type: TYPE; Schema: public; Owner: lka
--

CREATE TYPE public.job_stage AS ENUM (
    'extract',
    'ocr',
    'chunk',
    'entities',
    'embed',
    'summarize',
    'finalize'
);


ALTER TYPE public.job_stage OWNER TO lka;

--
-- Name: job_status; Type: TYPE; Schema: public; Owner: lka
--

CREATE TYPE public.job_status AS ENUM (
    'queued',
    'running',
    'done',
    'error'
);


ALTER TYPE public.job_status OWNER TO lka;

--
-- Name: pipeline_status; Type: TYPE; Schema: public; Owner: lka
--

CREATE TYPE public.pipeline_status AS ENUM (
    'queued',
    'extracting',
    'extracted',
    'ocr_running',
    'ocr_done',
    'chunking',
    'chunked',
    'extracting_entities',
    'entities_done',
    'embedding',
    'embedded',
    'summarizing',
    'summarized',
    'finalizing',
    'ready',
    'error'
);


ALTER TYPE public.pipeline_status OWNER TO lka;

--
-- Name: upload_source; Type: TYPE; Schema: public; Owner: lka
--

CREATE TYPE public.upload_source AS ENUM (
    'web',
    'watch_folder'
);


ALTER TYPE public.upload_source OWNER TO lka;

--
-- Name: user_role; Type: TYPE; Schema: public; Owner: lka
--

CREATE TYPE public.user_role AS ENUM (
    'admin',
    'user'
);


ALTER TYPE public.user_role OWNER TO lka;

--
-- Name: watched_file_status; Type: TYPE; Schema: public; Owner: lka
--

CREATE TYPE public.watched_file_status AS ENUM (
    'active',
    'removed'
);


ALTER TYPE public.watched_file_status OWNER TO lka;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


ALTER TABLE public.alembic_version OWNER TO lka;

--
-- Name: api_keys; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.api_keys (
    key_id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    key_hash text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    expires_at timestamp with time zone,
    permission_tier text DEFAULT 'full'::text NOT NULL,
    tool_overrides jsonb DEFAULT '{}'::jsonb NOT NULL,
    scope_topic_ids jsonb,
    scope_folder_ids jsonb,
    max_snippet_chars integer,
    rate_limit_rpm integer,
    rate_limit_rph integer
);


ALTER TABLE public.api_keys OWNER TO lka;

--
-- Name: api_request_log; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.api_request_log (
    request_id uuid DEFAULT gen_random_uuid() NOT NULL,
    api_key_id uuid,
    request_type text NOT NULL,
    endpoint text NOT NULL,
    parameters jsonb,
    status text NOT NULL,
    status_detail text,
    result_summary jsonb,
    duration_ms integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.api_request_log OWNER TO lka;

--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.audit_log (
    audit_id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid,
    api_key_id uuid,
    action text NOT NULL,
    target_type text,
    target_id uuid,
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.audit_log OWNER TO lka;

--
-- Name: chat_messages; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.chat_messages (
    message_id uuid DEFAULT gen_random_uuid() NOT NULL,
    conversation_id uuid NOT NULL,
    role character varying(20) NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    tool_calls jsonb,
    tool_call_id character varying(100),
    rag_context jsonb,
    tokens_used integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    model_id character varying(50),
    context_pct smallint
);


ALTER TABLE public.chat_messages OWNER TO lka;

--
-- Name: chunks; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.chunks (
    chunk_id uuid DEFAULT gen_random_uuid() NOT NULL,
    doc_id uuid NOT NULL,
    chunk_num integer NOT NULL,
    page_start integer,
    page_end integer,
    char_start integer,
    char_end integer,
    chunk_text text NOT NULL,
    language text DEFAULT 'english'::text NOT NULL,
    ocr_used boolean DEFAULT false NOT NULL,
    ocr_confidence double precision,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    fts_en tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig, COALESCE(chunk_text, ''::text))) STORED,
    fts_fr tsvector GENERATED ALWAYS AS (to_tsvector('french'::regconfig, COALESCE(chunk_text, ''::text))) STORED,
    embedding public.vector(384)
);


ALTER TABLE public.chunks OWNER TO lka;

--
-- Name: conversations; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.conversations (
    conversation_id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    title character varying(200) DEFAULT 'New conversation'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    mode character varying(10) DEFAULT 'chat'::character varying NOT NULL
);


ALTER TABLE public.conversations OWNER TO lka;

--
-- Name: corpus_topics; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.corpus_topics (
    topic_id integer NOT NULL,
    label text NOT NULL,
    keywords text[] NOT NULL,
    doc_count integer NOT NULL,
    representative_doc_ids uuid[] NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


ALTER TABLE public.corpus_topics OWNER TO lka;

--
-- Name: corpus_topics_meta; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.corpus_topics_meta (
    id integer DEFAULT 1 NOT NULL,
    last_computed_at timestamp with time zone,
    corpus_hash text
);


ALTER TABLE public.corpus_topics_meta OWNER TO lka;

--
-- Name: corpus_topics_topic_id_seq; Type: SEQUENCE; Schema: public; Owner: lka
--

CREATE SEQUENCE public.corpus_topics_topic_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.corpus_topics_topic_id_seq OWNER TO lka;

--
-- Name: corpus_topics_topic_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: lka
--

ALTER SEQUENCE public.corpus_topics_topic_id_seq OWNED BY public.corpus_topics.topic_id;


--
-- Name: document_headings; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.document_headings (
    heading_id uuid DEFAULT gen_random_uuid() NOT NULL,
    level integer NOT NULL,
    title text NOT NULL,
    page_num integer,
    "position" integer NOT NULL,
    doc_id uuid NOT NULL
);


ALTER TABLE public.document_headings OWNER TO lka;

--
-- Name: document_pages; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.document_pages (
    page_id uuid DEFAULT gen_random_uuid() NOT NULL,
    page_num integer NOT NULL,
    page_text text DEFAULT ''::text NOT NULL,
    ocr_used boolean DEFAULT false NOT NULL,
    ocr_confidence double precision,
    char_count integer GENERATED ALWAYS AS (char_length(page_text)) STORED,
    doc_id uuid NOT NULL
);


ALTER TABLE public.document_pages OWNER TO lka;

--
-- Name: documents; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.documents (
    doc_id uuid DEFAULT gen_random_uuid() NOT NULL,
    title text NOT NULL,
    canonical_filename text,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    topic_id integer,
    sha256 bytea NOT NULL,
    pipeline_status public.pipeline_status NOT NULL,
    pipeline_seq integer DEFAULT 0 NOT NULL,
    summary text,
    summary_model text,
    doc_type text,
    mime_type text,
    source_path text,
    error text,
    original_bucket text,
    original_object_key text,
    has_text_layer boolean,
    needs_ocr boolean,
    extracted_chars bigint,
    size_bytes bigint,
    ocr_languages_used text[],
    email_message_id text,
    email_thread_id text,
    email_parent_doc_id uuid,
    email_from_address text,
    email_from_name text,
    email_to_addresses text[],
    email_cc_addresses text[],
    email_date_sent timestamp with time zone,
    email_label_path text
);


ALTER TABLE public.documents OWNER TO lka;

--
-- Name: entities; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.entities (
    entity_id uuid DEFAULT gen_random_uuid() NOT NULL,
    chunk_id uuid NOT NULL,
    doc_id uuid NOT NULL,
    entity_text text NOT NULL,
    entity_type text NOT NULL,
    start_char integer NOT NULL,
    end_char integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.entities OWNER TO lka;

--
-- Name: imap_command_log; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.imap_command_log (
    log_id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    label_path text,
    command text NOT NULL,
    args_redacted text,
    response_status text NOT NULL,
    response_bytes integer DEFAULT 0 NOT NULL,
    duration_ms integer DEFAULT 0 NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.imap_command_log OWNER TO lka;

--
-- Name: ingestion_jobs; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.ingestion_jobs (
    job_id uuid DEFAULT gen_random_uuid() NOT NULL,
    stage public.job_stage NOT NULL,
    status public.job_status DEFAULT 'queued'::public.job_status NOT NULL,
    progress_current integer DEFAULT 0,
    progress_total integer DEFAULT 0,
    metrics jsonb DEFAULT '{}'::jsonb NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    heartbeat_at timestamp with time zone,
    priority smallint DEFAULT '0'::smallint NOT NULL,
    doc_id uuid NOT NULL
);


ALTER TABLE public.ingestion_jobs OWNER TO lka;

--
-- Name: mail_accounts; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.mail_accounts (
    account_id uuid DEFAULT gen_random_uuid() NOT NULL,
    display_name text NOT NULL,
    provider text NOT NULL,
    imap_host text NOT NULL,
    imap_port integer DEFAULT 993 NOT NULL,
    imap_username text NOT NULL,
    app_password_ciphertext bytea NOT NULL,
    key_fingerprint bytea NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    last_error text,
    last_connected_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.mail_accounts OWNER TO lka;

--
-- Name: model_settings; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.model_settings (
    model_id character varying(50) NOT NULL,
    settings jsonb DEFAULT '{}'::jsonb NOT NULL
);


ALTER TABLE public.model_settings OWNER TO lka;

--
-- Name: oauth_clients; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.oauth_clients (
    client_id uuid DEFAULT gen_random_uuid() NOT NULL,
    client_secret_hash text,
    client_name text,
    redirect_uris jsonb NOT NULL,
    grant_types jsonb NOT NULL,
    response_types jsonb NOT NULL,
    scope text DEFAULT 'mcp'::text NOT NULL,
    client_uri text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.oauth_clients OWNER TO lka;

--
-- Name: oauth_codes; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.oauth_codes (
    code_id uuid DEFAULT gen_random_uuid() NOT NULL,
    code_hash text NOT NULL,
    client_id uuid NOT NULL,
    user_id uuid NOT NULL,
    redirect_uri text NOT NULL,
    scope text NOT NULL,
    code_challenge text NOT NULL,
    code_challenge_method text DEFAULT 'S256'::text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.oauth_codes OWNER TO lka;

--
-- Name: oauth_tokens; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.oauth_tokens (
    token_id uuid DEFAULT gen_random_uuid() NOT NULL,
    access_token_hash text NOT NULL,
    refresh_token_hash text,
    client_id uuid NOT NULL,
    user_id uuid NOT NULL,
    scope text NOT NULL,
    access_token_expires_at timestamp with time zone NOT NULL,
    refresh_token_expires_at timestamp with time zone,
    revoked boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone
);


ALTER TABLE public.oauth_tokens OWNER TO lka;

--
-- Name: research_state; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.research_state (
    conversation_id uuid NOT NULL,
    strategy character varying(10) NOT NULL,
    status character varying(15) NOT NULL,
    notes text,
    current_round integer DEFAULT 0 NOT NULL,
    max_rounds integer NOT NULL,
    progress jsonb,
    completed_at timestamp with time zone,
    error text,
    heartbeat_at timestamp with time zone,
    time_limit_minutes integer,
    depth character varying(10),
    citations jsonb
);


ALTER TABLE public.research_state OWNER TO lka;

--
-- Name: upload_sessions; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.upload_sessions (
    session_id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    label text,
    auto_confirm boolean DEFAULT false NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    total_files integer DEFAULT 0 NOT NULL,
    uploaded integer DEFAULT 0 NOT NULL,
    confirmed integer DEFAULT 0 NOT NULL,
    failed integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.upload_sessions OWNER TO lka;

--
-- Name: uploads; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.uploads (
    upload_id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid,
    source public.upload_source DEFAULT 'web'::public.upload_source NOT NULL,
    original_filename text NOT NULL,
    mime_type text,
    size_bytes bigint,
    sha256 bytea,
    minio_bucket text NOT NULL,
    minio_object_key text NOT NULL,
    doc_id uuid,
    session_id uuid,
    source_path text,
    status text DEFAULT 'queued'::text NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.uploads OWNER TO lka;

--
-- Name: users; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.users (
    user_id uuid DEFAULT gen_random_uuid() NOT NULL,
    email public.citext NOT NULL,
    password_hash text NOT NULL,
    role public.user_role DEFAULT 'user'::public.user_role NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_login_at timestamp with time zone,
    preferences jsonb DEFAULT '{}'::jsonb NOT NULL,
    password_changed_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.users OWNER TO lka;

--
-- Name: watched_files; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.watched_files (
    file_id uuid DEFAULT gen_random_uuid() NOT NULL,
    folder_id uuid NOT NULL,
    relative_path text NOT NULL,
    bookmark_data bytea NOT NULL,
    sha256 bytea NOT NULL,
    doc_id uuid,
    status public.watched_file_status DEFAULT 'active'::public.watched_file_status NOT NULL,
    removed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.watched_files OWNER TO lka;

--
-- Name: watched_folders; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.watched_folders (
    folder_id uuid DEFAULT gen_random_uuid() NOT NULL,
    path text NOT NULL,
    bookmark_data bytea,
    recursive boolean DEFAULT true NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    last_event_id bigint,
    last_scan_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    unavailable_reason text,
    display_name text,
    auto_discovered boolean DEFAULT false NOT NULL
);


ALTER TABLE public.watched_folders OWNER TO lka;

--
-- Name: watched_labels; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.watched_labels (
    label_id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    label_path text NOT NULL,
    display_name text NOT NULL,
    uidvalidity bigint,
    last_uid_seen bigint DEFAULT '0'::bigint NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    last_error text,
    last_synced_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.watched_labels OWNER TO lka;

--
-- Name: watched_messages; Type: TABLE; Schema: public; Owner: lka
--

CREATE TABLE public.watched_messages (
    message_pk uuid DEFAULT gen_random_uuid() NOT NULL,
    label_id uuid NOT NULL,
    message_id text NOT NULL,
    imap_uid bigint NOT NULL,
    eml_sha256 bytea NOT NULL,
    email_doc_id uuid,
    status text DEFAULT 'active'::text NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    unlabeled_at timestamp with time zone
);


ALTER TABLE public.watched_messages OWNER TO lka;

--
-- Name: corpus_topics topic_id; Type: DEFAULT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.corpus_topics ALTER COLUMN topic_id SET DEFAULT nextval('public.corpus_topics_topic_id_seq'::regclass);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: api_keys api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (key_id);


--
-- Name: api_request_log api_request_log_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.api_request_log
    ADD CONSTRAINT api_request_log_pkey PRIMARY KEY (request_id);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (audit_id);


--
-- Name: chat_messages chat_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_pkey PRIMARY KEY (message_id);


--
-- Name: chunks chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_pkey PRIMARY KEY (chunk_id);


--
-- Name: conversations conversations_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_pkey PRIMARY KEY (conversation_id);


--
-- Name: corpus_topics_meta corpus_topics_meta_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.corpus_topics_meta
    ADD CONSTRAINT corpus_topics_meta_pkey PRIMARY KEY (id);


--
-- Name: corpus_topics corpus_topics_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.corpus_topics
    ADD CONSTRAINT corpus_topics_pkey PRIMARY KEY (topic_id);


--
-- Name: document_headings document_headings_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.document_headings
    ADD CONSTRAINT document_headings_pkey PRIMARY KEY (heading_id);


--
-- Name: document_pages document_pages_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.document_pages
    ADD CONSTRAINT document_pages_pkey PRIMARY KEY (page_id);


--
-- Name: documents documents_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_pkey PRIMARY KEY (doc_id);


--
-- Name: entities entities_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.entities
    ADD CONSTRAINT entities_pkey PRIMARY KEY (entity_id);


--
-- Name: imap_command_log imap_command_log_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.imap_command_log
    ADD CONSTRAINT imap_command_log_pkey PRIMARY KEY (log_id);


--
-- Name: ingestion_jobs ingestion_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.ingestion_jobs
    ADD CONSTRAINT ingestion_jobs_pkey PRIMARY KEY (job_id);


--
-- Name: mail_accounts mail_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.mail_accounts
    ADD CONSTRAINT mail_accounts_pkey PRIMARY KEY (account_id);


--
-- Name: model_settings model_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.model_settings
    ADD CONSTRAINT model_settings_pkey PRIMARY KEY (model_id);


--
-- Name: oauth_clients oauth_clients_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.oauth_clients
    ADD CONSTRAINT oauth_clients_pkey PRIMARY KEY (client_id);


--
-- Name: oauth_codes oauth_codes_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.oauth_codes
    ADD CONSTRAINT oauth_codes_pkey PRIMARY KEY (code_id);


--
-- Name: oauth_tokens oauth_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.oauth_tokens
    ADD CONSTRAINT oauth_tokens_pkey PRIMARY KEY (token_id);


--
-- Name: research_state research_state_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.research_state
    ADD CONSTRAINT research_state_pkey PRIMARY KEY (conversation_id);


--
-- Name: upload_sessions upload_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.upload_sessions
    ADD CONSTRAINT upload_sessions_pkey PRIMARY KEY (session_id);


--
-- Name: uploads uploads_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.uploads
    ADD CONSTRAINT uploads_pkey PRIMARY KEY (upload_id);


--
-- Name: chunks uq_chunks_doc_num; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT uq_chunks_doc_num UNIQUE (doc_id, chunk_num);


--
-- Name: ingestion_jobs uq_jobs_doc_stage; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.ingestion_jobs
    ADD CONSTRAINT uq_jobs_doc_stage UNIQUE (doc_id, stage);


--
-- Name: mail_accounts uq_mail_accounts_host_username; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.mail_accounts
    ADD CONSTRAINT uq_mail_accounts_host_username UNIQUE (imap_host, imap_username);


--
-- Name: document_pages uq_pages_doc_page; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.document_pages
    ADD CONSTRAINT uq_pages_doc_page UNIQUE (doc_id, page_num);


--
-- Name: users uq_users_email; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT uq_users_email UNIQUE (email);


--
-- Name: watched_files uq_watched_files_folder_path; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.watched_files
    ADD CONSTRAINT uq_watched_files_folder_path UNIQUE (folder_id, relative_path);


--
-- Name: watched_labels uq_watched_labels_account_path; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.watched_labels
    ADD CONSTRAINT uq_watched_labels_account_path UNIQUE (account_id, label_path);


--
-- Name: watched_messages uq_watched_messages_label_message; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.watched_messages
    ADD CONSTRAINT uq_watched_messages_label_message UNIQUE (label_id, message_id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (user_id);


--
-- Name: watched_files watched_files_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.watched_files
    ADD CONSTRAINT watched_files_pkey PRIMARY KEY (file_id);


--
-- Name: watched_folders watched_folders_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.watched_folders
    ADD CONSTRAINT watched_folders_pkey PRIMARY KEY (folder_id);


--
-- Name: watched_labels watched_labels_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.watched_labels
    ADD CONSTRAINT watched_labels_pkey PRIMARY KEY (label_id);


--
-- Name: watched_messages watched_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.watched_messages
    ADD CONSTRAINT watched_messages_pkey PRIMARY KEY (message_pk);


--
-- Name: api_keys_active_idx; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX api_keys_active_idx ON public.api_keys USING btree (is_active);


--
-- Name: audit_created_idx; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX audit_created_idx ON public.audit_log USING btree (created_at DESC);


--
-- Name: chunks_doc_embedding_idx; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX chunks_doc_embedding_idx ON public.chunks USING btree (doc_id) WHERE (embedding IS NOT NULL);


--
-- Name: chunks_doc_idx; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX chunks_doc_idx ON public.chunks USING btree (doc_id);


--
-- Name: chunks_embedding_hnsw_idx; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX chunks_embedding_hnsw_idx ON public.chunks USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: chunks_fts_en_idx; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX chunks_fts_en_idx ON public.chunks USING gin (fts_en);


--
-- Name: chunks_fts_fr_idx; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX chunks_fts_fr_idx ON public.chunks USING gin (fts_fr);


--
-- Name: chunks_language_idx; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX chunks_language_idx ON public.chunks USING btree (language);


--
-- Name: documents_status_updated_idx; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX documents_status_updated_idx ON public.documents USING btree (status, updated_at DESC);


--
-- Name: documents_updated_idx; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX documents_updated_idx ON public.documents USING btree (updated_at DESC);


--
-- Name: idx_messages_conv; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX idx_messages_conv ON public.chat_messages USING btree (conversation_id, created_at);


--
-- Name: ix_api_request_log_created; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_api_request_log_created ON public.api_request_log USING btree (created_at);


--
-- Name: ix_api_request_log_endpoint; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_api_request_log_endpoint ON public.api_request_log USING btree (endpoint);


--
-- Name: ix_api_request_log_key_time_ep; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_api_request_log_key_time_ep ON public.api_request_log USING btree (api_key_id, created_at DESC, endpoint);


--
-- Name: ix_conversations_user_updated; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_conversations_user_updated ON public.conversations USING btree (user_id, updated_at);


--
-- Name: ix_documents_email_message_id; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_documents_email_message_id ON public.documents USING btree (email_message_id) WHERE (email_message_id IS NOT NULL);


--
-- Name: ix_documents_email_parent; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_documents_email_parent ON public.documents USING btree (email_parent_doc_id) WHERE (email_parent_doc_id IS NOT NULL);


--
-- Name: ix_documents_email_thread_id; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_documents_email_thread_id ON public.documents USING btree (email_thread_id) WHERE (email_thread_id IS NOT NULL);


--
-- Name: ix_entities_chunk_id; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_entities_chunk_id ON public.entities USING btree (chunk_id);


--
-- Name: ix_entities_doc_id; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_entities_doc_id ON public.entities USING btree (doc_id);


--
-- Name: ix_entities_text_chunk; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_entities_text_chunk ON public.entities USING btree (entity_text, chunk_id);


--
-- Name: ix_entities_text_trgm; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_entities_text_trgm ON public.entities USING gin (entity_text public.gin_trgm_ops);


--
-- Name: ix_entities_type_text; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_entities_type_text ON public.entities USING btree (entity_type, entity_text);


--
-- Name: ix_imap_command_log_account_time; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_imap_command_log_account_time ON public.imap_command_log USING btree (account_id, created_at DESC);


--
-- Name: ix_imap_command_log_created; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_imap_command_log_created ON public.imap_command_log USING btree (created_at);


--
-- Name: ix_ingestion_jobs_priority_created; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_ingestion_jobs_priority_created ON public.ingestion_jobs USING btree (priority, created_at);


--
-- Name: ix_oauth_codes_code_hash; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_oauth_codes_code_hash ON public.oauth_codes USING btree (code_hash);


--
-- Name: ix_oauth_tokens_access_hash; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_oauth_tokens_access_hash ON public.oauth_tokens USING btree (access_token_hash);


--
-- Name: ix_oauth_tokens_refresh_hash; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_oauth_tokens_refresh_hash ON public.oauth_tokens USING btree (refresh_token_hash);


--
-- Name: ix_research_state_one_running; Type: INDEX; Schema: public; Owner: lka
--

CREATE UNIQUE INDEX ix_research_state_one_running ON public.research_state USING btree (status) WHERE ((status)::text = 'running'::text);


--
-- Name: ix_uploads_session_id; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_uploads_session_id ON public.uploads USING btree (session_id);


--
-- Name: ix_watched_files_folder_id; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_watched_files_folder_id ON public.watched_files USING btree (folder_id);


--
-- Name: ix_watched_files_status; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_watched_files_status ON public.watched_files USING btree (status);


--
-- Name: ix_watched_messages_eml_sha; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_watched_messages_eml_sha ON public.watched_messages USING btree (eml_sha256);


--
-- Name: ix_watched_messages_label_status; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX ix_watched_messages_label_status ON public.watched_messages USING btree (label_id, status);


--
-- Name: jobs_status_idx; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX jobs_status_idx ON public.ingestion_jobs USING btree (status);


--
-- Name: uploads_created_idx; Type: INDEX; Schema: public; Owner: lka
--

CREATE INDEX uploads_created_idx ON public.uploads USING btree (created_at DESC);


--
-- Name: api_request_log api_request_log_api_key_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.api_request_log
    ADD CONSTRAINT api_request_log_api_key_id_fkey FOREIGN KEY (api_key_id) REFERENCES public.api_keys(key_id) ON DELETE SET NULL;


--
-- Name: audit_log audit_log_api_key_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_api_key_id_fkey FOREIGN KEY (api_key_id) REFERENCES public.api_keys(key_id) ON DELETE SET NULL;


--
-- Name: audit_log audit_log_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(user_id) ON DELETE SET NULL;


--
-- Name: chat_messages chat_messages_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.conversations(conversation_id) ON DELETE CASCADE;


--
-- Name: chunks chunks_doc_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_doc_id_fkey FOREIGN KEY (doc_id) REFERENCES public.documents(doc_id) ON DELETE CASCADE;


--
-- Name: conversations conversations_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(user_id) ON DELETE CASCADE;


--
-- Name: documents documents_email_parent_doc_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_email_parent_doc_id_fkey FOREIGN KEY (email_parent_doc_id) REFERENCES public.documents(doc_id) ON DELETE SET NULL;


--
-- Name: documents documents_topic_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_topic_id_fkey FOREIGN KEY (topic_id) REFERENCES public.corpus_topics(topic_id) ON DELETE SET NULL;


--
-- Name: entities entities_chunk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.entities
    ADD CONSTRAINT entities_chunk_id_fkey FOREIGN KEY (chunk_id) REFERENCES public.chunks(chunk_id) ON DELETE CASCADE;


--
-- Name: entities entities_doc_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.entities
    ADD CONSTRAINT entities_doc_id_fkey FOREIGN KEY (doc_id) REFERENCES public.documents(doc_id) ON DELETE CASCADE;


--
-- Name: document_headings fk_headings_doc_id; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.document_headings
    ADD CONSTRAINT fk_headings_doc_id FOREIGN KEY (doc_id) REFERENCES public.documents(doc_id) ON DELETE CASCADE;


--
-- Name: ingestion_jobs fk_jobs_doc_id; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.ingestion_jobs
    ADD CONSTRAINT fk_jobs_doc_id FOREIGN KEY (doc_id) REFERENCES public.documents(doc_id) ON DELETE CASCADE;


--
-- Name: document_pages fk_pages_doc_id; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.document_pages
    ADD CONSTRAINT fk_pages_doc_id FOREIGN KEY (doc_id) REFERENCES public.documents(doc_id) ON DELETE CASCADE;


--
-- Name: imap_command_log imap_command_log_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.imap_command_log
    ADD CONSTRAINT imap_command_log_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.mail_accounts(account_id) ON DELETE CASCADE;


--
-- Name: oauth_codes oauth_codes_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.oauth_codes
    ADD CONSTRAINT oauth_codes_client_id_fkey FOREIGN KEY (client_id) REFERENCES public.oauth_clients(client_id);


--
-- Name: oauth_codes oauth_codes_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.oauth_codes
    ADD CONSTRAINT oauth_codes_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(user_id);


--
-- Name: oauth_tokens oauth_tokens_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.oauth_tokens
    ADD CONSTRAINT oauth_tokens_client_id_fkey FOREIGN KEY (client_id) REFERENCES public.oauth_clients(client_id);


--
-- Name: oauth_tokens oauth_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.oauth_tokens
    ADD CONSTRAINT oauth_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(user_id);


--
-- Name: research_state research_state_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.research_state
    ADD CONSTRAINT research_state_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.conversations(conversation_id) ON DELETE CASCADE;


--
-- Name: upload_sessions upload_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.upload_sessions
    ADD CONSTRAINT upload_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(user_id) ON DELETE CASCADE;


--
-- Name: uploads uploads_doc_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.uploads
    ADD CONSTRAINT uploads_doc_id_fkey FOREIGN KEY (doc_id) REFERENCES public.documents(doc_id) ON DELETE SET NULL;


--
-- Name: uploads uploads_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.uploads
    ADD CONSTRAINT uploads_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.upload_sessions(session_id) ON DELETE SET NULL;


--
-- Name: uploads uploads_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.uploads
    ADD CONSTRAINT uploads_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(user_id) ON DELETE SET NULL;


--
-- Name: watched_files watched_files_doc_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.watched_files
    ADD CONSTRAINT watched_files_doc_id_fkey FOREIGN KEY (doc_id) REFERENCES public.documents(doc_id) ON DELETE SET NULL;


--
-- Name: watched_files watched_files_folder_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.watched_files
    ADD CONSTRAINT watched_files_folder_id_fkey FOREIGN KEY (folder_id) REFERENCES public.watched_folders(folder_id) ON DELETE CASCADE;


--
-- Name: watched_labels watched_labels_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.watched_labels
    ADD CONSTRAINT watched_labels_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.mail_accounts(account_id) ON DELETE CASCADE;


--
-- Name: watched_messages watched_messages_email_doc_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.watched_messages
    ADD CONSTRAINT watched_messages_email_doc_id_fkey FOREIGN KEY (email_doc_id) REFERENCES public.documents(doc_id) ON DELETE SET NULL;


--
-- Name: watched_messages watched_messages_label_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lka
--

ALTER TABLE ONLY public.watched_messages
    ADD CONSTRAINT watched_messages_label_id_fkey FOREIGN KEY (label_id) REFERENCES public.watched_labels(label_id) ON DELETE CASCADE;


--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: lka
--

REVOKE USAGE ON SCHEMA public FROM PUBLIC;


--
-- PostgreSQL database dump complete
--

\unrestrict HibCzWg7RyoRD2yGl5tJuWjhPYNL0TqVY6wWBF0dKceynyy3ag8O8MRcNodlkUQ

