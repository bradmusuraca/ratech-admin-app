-- Run in Supabase: SQL Editor -> New query -> paste -> Run
-- Creates the order queue the app writes to and the bridge reads.

create table if not exists sage_order_queue (
    order_key      text primary key,
    status         text not null default 'pending'
                   check (status in ('pending','processing','done','error')),
    payload        jsonb not null,
    sage_order_no  text,
    error          text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

-- service_role key bypasses RLS entirely; no policies needed since only
-- the app (server-side) and bridge hold that key.
alter table sage_order_queue enable row level security;

-- Distinguishes 'create' sales-order jobs from 'qty_update' edits the
-- packager's short-ship marks generate.
alter table sage_order_queue add column if not exists job_type text not null default 'create';

-- Packing list: one row per Sage transaction sent to the floor.
create table if not exists open_orders (
    order_key      text primary key,
    customer_id    text,
    customer_po    text,
    status         text not null default 'open'
                   check (status in ('open','short','complete')),
    lines          jsonb not null,
    sage_order_no  text,
    packed_by      text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

alter table open_orders enable row level security;

-- Re-saved jobs get 'superseded' - allow it in the status check.
alter table sage_order_queue drop constraint if exists sage_order_queue_status_check;
alter table sage_order_queue add constraint sage_order_queue_status_check
    check (status in ('pending','processing','done','error','superseded'));

-- Source document (order PDF) kept per order so the packing page can show
-- the original paperwork next to the freight widget. Base64 text keeps it
-- in Postgres - no Storage bucket to configure.
create table if not exists order_docs (
    order_key      text primary key,
    file_name      text,
    pdf_b64        text,
    created_at     timestamptz not null default now()
);

alter table order_docs enable row level security;

-- Third-party FedEx billing per customer: when set, the Create Invoices
-- freight widget auto-fills the customer's FedEx account.
create table if not exists fedex_profiles (
    customer_id    text primary key,
    acct           text,
    zip            text,
    created_at     timestamptz not null default now()
);

alter table fedex_profiles enable row level security;
